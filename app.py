from flask import Flask, render_template, request, redirect, url_for, flash, session, Response, send_file
import sqlite3
import os
from pathlib import Path
from datetime import datetime, timedelta
import calendar
import csv
import io
import zipfile
import holidays
from functools import lru_cache
from io import BytesIO

app = Flask(__name__)
app.secret_key = os.environ.get(
    "SECRET_KEY",
    "controle-financeiro-local"
)

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = Path(
    os.environ.get(
        "DATA_DIR",
        str(BASE_DIR)
    )
)

BACKUP_DIR = Path(
    os.environ.get(
        "BACKUP_DIR",
        str(BASE_DIR / "backups")
    )
)

DATA_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "despesas.db"
MAX_BACKUPS = 30

CATEGORIAS_DESPESAS = [
    "Alimentação",
    "Casa",
    "Educação",
    "Lazer",
    "Moradia",
    "Saúde",
    "Transporte",
    "Contas",
    "Compras",
    "Outros",
]

CATEGORIAS_RECEITAS = [
    "Salário",
    "Freelance",
    "Investimentos",
    "Venda",
    "Presente",
    "Reembolso",
    "Outros",
]

MESES = {
    "01": "Janeiro",
    "02": "Fevereiro",
    "03": "Março",
    "04": "Abril",
    "05": "Maio",
    "06": "Junho",
    "07": "Julho",
    "08": "Agosto",
    "09": "Setembro",
    "10": "Outubro",
    "11": "Novembro",
    "12": "Dezembro",
}



def criar_backup_automatico():
    """Cria no máximo um backup por dia e mantém os 30 mais recentes."""
    if not DB_PATH.exists():
        return None

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    hoje = datetime.now().strftime("%Y-%m-%d")
    destino = BACKUP_DIR / f"despesas_backup_{hoje}.db"

    # Um backup diário é suficiente. Se já existe, não sobrescreve.
    if destino.exists():
        return destino

    temporario = BACKUP_DIR / f".backup_{hoje}.tmp"

    try:
        if temporario.exists():
            temporario.unlink()

        origem = sqlite3.connect(DB_PATH)
        copia = sqlite3.connect(temporario)

        try:
            origem.backup(copia)
        finally:
            copia.close()
            origem.close()

        temporario.replace(destino)

        backups = sorted(
            BACKUP_DIR.glob("despesas_backup_*.db"),
            key=lambda arquivo: arquivo.stat().st_mtime,
            reverse=True,
        )

        for antigo in backups[MAX_BACKUPS:]:
            try:
                antigo.unlink()
            except OSError:
                pass

        return destino

    except Exception as erro:
        # Falha no backup não deve impedir o sistema de abrir.
        print(f"Aviso: não foi possível criar o backup automático: {erro}")

        try:
            if temporario.exists():
                temporario.unlink()
        except OSError:
            pass

        return None


def obter_ultimo_backup():
    if not BACKUP_DIR.exists():
        return "Ainda não criado"

    backups = sorted(
        BACKUP_DIR.glob("despesas_backup_*.db"),
        key=lambda arquivo: arquivo.stat().st_mtime,
        reverse=True,
    )

    if not backups:
        return "Ainda não criado"

    data = datetime.fromtimestamp(
        backups[0].stat().st_mtime
    )

    return data.strftime("%d/%m/%Y %H:%M")


def conectar():
    conexao = sqlite3.connect(DB_PATH)
    conexao.row_factory = sqlite3.Row
    return conexao


def formatar_moeda(valor):
    try:
        valor = float(valor or 0)
    except (TypeError, ValueError):
        valor = 0

    texto = f"{valor:,.2f}"
    texto = texto.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {texto}"


app.jinja_env.filters["moeda"] = formatar_moeda


@app.before_request
def tarefas_automaticas():
    criar_backup_automatico()

    if (
        request.endpoint == "static"
        or not DB_PATH.exists()
    ):
        return

    try:
        garantir_estrutura()
        processar_rendimentos_pendentes()
        sincronizar_faturas_cartao()
    except Exception as erro:
        # Um problema no rendimento nunca deve impedir
        # o restante do Controle Financeiro de abrir.
        print(
            "Aviso: não foi possível processar "
            f"o rendimento diário: {erro}"
        )


def garantir_estrutura():
    if not DB_PATH.exists():
        return

    with conectar() as conexao:
        cursor = conexao.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS despesas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                descricao TEXT NOT NULL,
                valor REAL NOT NULL,
                categoria TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS receitas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                descricao TEXT NOT NULL,
                valor REAL NOT NULL,
                categoria TEXT NOT NULL
            )
        """)

        for comando in [
            "ALTER TABLE despesas ADD COLUMN data TEXT",
            "ALTER TABLE receitas ADD COLUMN data TEXT",
            "ALTER TABLE despesas ADD COLUMN pago INTEGER DEFAULT 1",
            "ALTER TABLE despesas ADD COLUMN data_pagamento TEXT",
            "ALTER TABLE despesas ADD COLUMN despesa_fixa_id INTEGER",
            "ALTER TABLE despesas ADD COLUMN cartao_fatura_id INTEGER",
        ]:
            try:
                cursor.execute(comando)
            except sqlite3.OperationalError:
                pass

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS despesas_fixas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                descricao TEXT NOT NULL,
                valor REAL NOT NULL,
                categoria TEXT NOT NULL,
                dia_vencimento INTEGER NOT NULL,
                ativa INTEGER NOT NULL DEFAULT 1,
                data_inicio TEXT
            )
        """)

        try:
            cursor.execute("""
                ALTER TABLE despesas_fixas
                ADD COLUMN data_inicio TEXT
            """)
        except sqlite3.OperationalError:
            pass

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS despesas_fixas_excecoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                despesa_fixa_id INTEGER NOT NULL,
                competencia TEXT NOT NULL,
                UNIQUE(despesa_fixa_id, competencia)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS categorias (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL COLLATE NOCASE,
                tipo TEXT NOT NULL,
                UNIQUE(tipo, nome)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rendimento_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                ativo INTEGER NOT NULL DEFAULT 0,
                percentual_cdi REAL NOT NULL DEFAULT 100,
                taxa_anual REAL NOT NULL DEFAULT 14,
                data_inicio TEXT,
                processar_a_partir_de TEXT,
                atualizado_em TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rendimentos_diarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_referencia TEXT NOT NULL UNIQUE,
                data_credito TEXT NOT NULL,
                saldo_base REAL NOT NULL,
                taxa_anual REAL NOT NULL,
                percentual_cdi REAL NOT NULL,
                taxa_diaria REAL NOT NULL,
                rendimento REAL NOT NULL,
                criado_em TEXT NOT NULL
            )
        """)

        for comando in [
            "ALTER TABLE rendimento_config ADD COLUMN aplicar_ir INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE rendimentos_diarios ADD COLUMN rendimento_bruto REAL",
            "ALTER TABLE rendimentos_diarios ADD COLUMN aliquota_ir REAL",
            "ALTER TABLE rendimentos_diarios ADD COLUMN imposto_ir REAL",
            "ALTER TABLE rendimentos_diarios ADD COLUMN saldo_elegivel REAL",
        ]:
            try:
                cursor.execute(comando)
            except sqlite3.OperationalError:
                pass


        hoje_config = datetime.now().strftime("%d/%m/%Y")

        cursor.execute("""
            INSERT OR IGNORE INTO rendimento_config (
                id,
                ativo,
                percentual_cdi,
                taxa_anual,
                data_inicio,
                processar_a_partir_de,
                atualizado_em
            )
            VALUES (
                1,
                0,
                100,
                14,
                ?,
                ?,
                ?
            )
        """, (
            hoje_config,
            hoje_config,
            datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        ))

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cartao_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                nome TEXT NOT NULL DEFAULT 'Meu cartão',
                dia_fechamento INTEGER NOT NULL DEFAULT 20,
                dia_vencimento INTEGER NOT NULL DEFAULT 10,
                limite REAL NOT NULL DEFAULT 0,
                ativo INTEGER NOT NULL DEFAULT 0
            )
        """)

        # A partir desta versão, cada cartão possui sua própria configuração.
        # A tabela cartao_config é mantida somente como fonte de migração para
        # instalações antigas que tinham apenas um cartão.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cartoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                dia_fechamento INTEGER NOT NULL DEFAULT 20,
                dia_vencimento INTEGER NOT NULL DEFAULT 10,
                limite REAL NOT NULL DEFAULT 0,
                ativo INTEGER NOT NULL DEFAULT 0,
                criado_em TEXT NOT NULL
            )
        """)

        try:
            cursor.execute("""
                ALTER TABLE cartoes
                ADD COLUMN criado_em TEXT
            """)
        except sqlite3.OperationalError:
            pass

        cursor.execute("""
            UPDATE cartoes
            SET criado_em = COALESCE(criado_em, ?)
            WHERE criado_em IS NULL OR TRIM(criado_em) = ''
        """, (datetime.now().strftime("%d/%m/%Y %H:%M:%S"),))

        cursor.execute("""
            INSERT OR IGNORE INTO cartao_config (
                id,
                nome,
                dia_fechamento,
                dia_vencimento,
                limite,
                ativo
            )
            VALUES (1, 'Meu cartão', 20, 10, 0, 0)
        """)

        quantidade_cartoes = cursor.execute("""
            SELECT COUNT(*)
            FROM cartoes
        """).fetchone()[0]

        if int(quantidade_cartoes or 0) == 0:
            legado = cursor.execute("""
                SELECT
                    nome,
                    dia_fechamento,
                    dia_vencimento,
                    limite,
                    ativo
                FROM cartao_config
                WHERE id = 1
            """).fetchone()

            cursor.execute("""
                INSERT INTO cartoes (
                    nome,
                    dia_fechamento,
                    dia_vencimento,
                    limite,
                    ativo,
                    criado_em
                )
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                legado["nome"] if legado else "Meu cartão",
                legado["dia_fechamento"] if legado else 20,
                legado["dia_vencimento"] if legado else 10,
                legado["limite"] if legado else 0,
                legado["ativo"] if legado else 0,
                datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
            ))

        cartao_principal = cursor.execute("""
            SELECT id
            FROM cartoes
            ORDER BY id
            LIMIT 1
        """).fetchone()
        cartao_principal_id = int(cartao_principal["id"])

        # A versão anterior tinha UNIQUE(competencia), o que impediria dois
        # cartões de terem faturas na mesma competência. Recriamos a tabela
        # preservando os mesmos IDs, para manter as despesas já vinculadas.
        faturas_info = cursor.execute("PRAGMA table_info(cartao_faturas)").fetchall()
        faturas_colunas = {item["name"] for item in faturas_info}

        if not faturas_info:
            cursor.execute("""
                CREATE TABLE cartao_faturas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cartao_id INTEGER NOT NULL,
                    competencia TEXT NOT NULL,
                    data_fechamento TEXT NOT NULL,
                    data_vencimento TEXT NOT NULL,
                    pago INTEGER NOT NULL DEFAULT 0,
                    data_pagamento TEXT,
                    despesa_id INTEGER,
                    criado_em TEXT NOT NULL,
                    UNIQUE(cartao_id, competencia)
                )
            """)
        elif "cartao_id" not in faturas_colunas:
            cursor.execute("ALTER TABLE cartao_faturas RENAME TO cartao_faturas_legado")
            cursor.execute("""
                CREATE TABLE cartao_faturas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cartao_id INTEGER NOT NULL,
                    competencia TEXT NOT NULL,
                    data_fechamento TEXT NOT NULL,
                    data_vencimento TEXT NOT NULL,
                    pago INTEGER NOT NULL DEFAULT 0,
                    data_pagamento TEXT,
                    despesa_id INTEGER,
                    criado_em TEXT NOT NULL,
                    UNIQUE(cartao_id, competencia)
                )
            """)
            cursor.execute("""
                INSERT INTO cartao_faturas (
                    id,
                    cartao_id,
                    competencia,
                    data_fechamento,
                    data_vencimento,
                    pago,
                    data_pagamento,
                    despesa_id,
                    criado_em
                )
                SELECT
                    id,
                    ?,
                    competencia,
                    data_fechamento,
                    data_vencimento,
                    pago,
                    data_pagamento,
                    despesa_id,
                    criado_em
                FROM cartao_faturas_legado
            """, (cartao_principal_id,))
            cursor.execute("DROP TABLE cartao_faturas_legado")
        else:
            cursor.execute("""
                UPDATE cartao_faturas
                SET cartao_id = ?
                WHERE cartao_id IS NULL
            """, (cartao_principal_id,))
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                idx_cartao_faturas_cartao_competencia
                ON cartao_faturas(cartao_id, competencia)
            """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cartao_compras (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                descricao TEXT NOT NULL,
                valor REAL NOT NULL,
                categoria TEXT NOT NULL,
                data_compra TEXT NOT NULL,
                fatura_id INTEGER NOT NULL,
                criado_em TEXT NOT NULL,
                parcelas INTEGER NOT NULL DEFAULT 1
            )
        """)

        try:
            cursor.execute("""
                ALTER TABLE cartao_compras
                ADD COLUMN parcelas INTEGER NOT NULL DEFAULT 1
            """)
        except sqlite3.OperationalError:
            pass

        cursor.execute("""
            UPDATE cartao_compras
            SET parcelas = 1
            WHERE parcelas IS NULL OR parcelas < 1
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cartao_parcelas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                compra_id INTEGER NOT NULL,
                numero_parcela INTEGER NOT NULL,
                valor REAL NOT NULL,
                fatura_id INTEGER NOT NULL,
                UNIQUE(compra_id, numero_parcela)
            )
        """)


        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cartao_servicos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cartao_id INTEGER NOT NULL,
                descricao TEXT NOT NULL,
                valor REAL NOT NULL,
                categoria TEXT NOT NULL,
                data_inicio TEXT NOT NULL,
                ativo INTEGER NOT NULL DEFAULT 1,
                criado_em TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cartao_servicos_faturas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                servico_id INTEGER,
                fatura_id INTEGER NOT NULL,
                descricao TEXT NOT NULL,
                valor REAL NOT NULL,
                categoria TEXT NOT NULL,
                criado_em TEXT NOT NULL,
                UNIQUE(servico_id, fatura_id)
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_cartao_servicos_cartao
            ON cartao_servicos(cartao_id, ativo)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_cartao_servicos_faturas_fatura
            ON cartao_servicos_faturas(fatura_id)
        """)

        # Compras da versão anterior são tratadas como uma parcela única.
        cursor.execute("""
            INSERT OR IGNORE INTO cartao_parcelas (
                compra_id,
                numero_parcela,
                valor,
                fatura_id
            )
            SELECT
                c.id,
                1,
                c.valor,
                c.fatura_id
            FROM cartao_compras c
            WHERE NOT EXISTS (
                SELECT 1
                FROM cartao_parcelas p
                WHERE p.compra_id = c.id
            )
        """)

        # Categorias padrão da aplicação.
        for nome in CATEGORIAS_DESPESAS:
            cursor.execute("""
                INSERT OR IGNORE INTO categorias (
                    nome,
                    tipo
                )
                VALUES (?, 'despesa')
            """, (nome,))

        for nome in CATEGORIAS_RECEITAS:
            cursor.execute("""
                INSERT OR IGNORE INTO categorias (
                    nome,
                    tipo
                )
                VALUES (?, 'receita')
            """, (nome,))

        cursor.execute("""
            INSERT OR IGNORE INTO categorias (
                nome,
                tipo
            )
            VALUES ('Cartão de Crédito', 'despesa')
        """)

        # Também importa automaticamente categorias que já
        # existiam no banco antes desta funcionalidade.
        for tabela in ("despesas", "despesas_fixas"):
            registros = cursor.execute(f"""
                SELECT DISTINCT categoria
                FROM {tabela}
                WHERE categoria IS NOT NULL
                  AND TRIM(categoria) <> ''
            """).fetchall()

            for registro in registros:
                cursor.execute("""
                    INSERT OR IGNORE INTO categorias (
                        nome,
                        tipo
                    )
                    VALUES (?, 'despesa')
                """, (registro[0].strip(),))

        registros = cursor.execute("""
            SELECT DISTINCT categoria
            FROM receitas
            WHERE categoria IS NOT NULL
              AND TRIM(categoria) <> ''
        """).fetchall()

        for registro in registros:
            cursor.execute("""
                INSERT OR IGNORE INTO categorias (
                    nome,
                    tipo
                )
                VALUES (?, 'receita')
            """, (registro[0].strip(),))

        cursor.execute("""
            UPDATE rendimento_config
            SET aplicar_ir = COALESCE(aplicar_ir, 1)
            WHERE id = 1
        """)

        cursor.execute("""
            UPDATE rendimento_config
            SET taxa_anual = 13.90
            WHERE id = 1
              AND ABS(COALESCE(taxa_anual, 14) - 14) < 0.0001
        """)

        conexao.commit()




def parse_data_banco(valor):
    if not valor:
        return None

    try:
        return datetime.strptime(
            valor,
            "%d/%m/%Y"
        ).date()
    except (TypeError, ValueError):
        return None


def formatar_data_banco(valor):
    if not valor:
        return None

    if hasattr(valor, "strftime"):
        return valor.strftime(
            "%d/%m/%Y"
        )

    return str(valor)


def calcular_pascoa(ano):
    """Algoritmo gregoriano para a data da Páscoa."""
    a = ano % 19
    b = ano // 100
    c = ano % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (
        19 * a
        + b
        - d
        - g
        + 15
    ) % 30
    i = c // 4
    k = c % 4
    l = (
        32
        + 2 * e
        + 2 * i
        - h
        - k
    ) % 7
    m = (
        a
        + 11 * h
        + 22 * l
    ) // 451

    mes = (
        h
        + l
        - 7 * m
        + 114
    ) // 31

    dia = (
        (
            h
            + l
            - 7 * m
            + 114
        ) % 31
    ) + 1

    return datetime(
        ano,
        mes,
        dia
    ).date()


def feriados_bancarios_brasil(ano):
    """
    Calendário prático para rendimento diário no Brasil.
    Inclui feriados nacionais e datas sem expediente bancário
    mais relevantes, além de Carnaval, Sexta-feira Santa e
    Corpus Christi.
    """
    pascoa = calcular_pascoa(
        ano
    )

    fixos = {
        datetime(ano, 1, 1).date(),
        datetime(ano, 4, 21).date(),
        datetime(ano, 5, 1).date(),
        datetime(ano, 9, 7).date(),
        datetime(ano, 10, 12).date(),
        datetime(ano, 11, 2).date(),
        datetime(ano, 11, 15).date(),
        datetime(ano, 11, 20).date(),
        datetime(ano, 12, 25).date(),
    }

    moveis = {
        pascoa - timedelta(days=48),  # Segunda de Carnaval
        pascoa - timedelta(days=47),  # Terça de Carnaval
        pascoa - timedelta(days=2),   # Sexta-feira Santa
        pascoa + timedelta(days=60),  # Corpus Christi
    }

    return fixos | moveis


def eh_dia_util_rendimento(data):
    if data.weekday() >= 5:
        return False

    return (
        data
        not in feriados_bancarios_brasil(
            data.year
        )
    )


def proximo_dia_util(data):
    candidato = (
        data
        + timedelta(days=1)
    )

    while not eh_dia_util_rendimento(
        candidato
    ):
        candidato += timedelta(days=1)

    return candidato


def obter_config_rendimento(conexao):
    registro = conexao.execute("""
        SELECT
            id,
            ativo,
            percentual_cdi,
            taxa_anual,
            data_inicio,
            processar_a_partir_de,
            atualizado_em,
            COALESCE(aplicar_ir, 1) AS aplicar_ir
        FROM rendimento_config
        WHERE id = 1
    """).fetchone()

    return (
        dict(registro)
        if registro
        else None
    )


def aliquota_ir_regressiva(
    data_inicio,
    data_referencia
):
    """Alíquota regressiva do IR de renda fixa, em percentual."""
    inicio = (
        parse_data_banco(data_inicio)
        if isinstance(data_inicio, str)
        else data_inicio
    )

    referencia = (
        parse_data_banco(data_referencia)
        if isinstance(data_referencia, str)
        else data_referencia
    )

    if not inicio or not referencia:
        return 22.5

    dias = max(
        (referencia - inicio).days,
        0
    )

    if dias <= 180:
        return 22.5
    if dias <= 360:
        return 20.0
    if dias <= 720:
        return 17.5
    return 15.0


def data_receita_fica_elegivel(
    data_receita
):
    data = parse_data_banco(
        data_receita
    )

    if not data:
        return None

    # Mercado Pago informa que depósitos entram no rendimento
    # a partir do próximo dia útil.
    return proximo_dia_util(
        data
    )


def taxa_diaria_rendimento(
    taxa_anual,
    percentual_cdi
):
    anual = max(
        float(taxa_anual or 0),
        0
    ) / 100

    percentual = max(
        float(percentual_cdi or 0),
        0
    ) / 100

    if anual <= 0 or percentual <= 0:
        return 0.0

    taxa_base_diaria = (
        (1 + anual) ** (1 / 252)
        - 1
    )

    return (
        taxa_base_diaria
        * percentual
    )


def soma_rendimentos_mes(
    conexao,
    mes,
    ano,
    ate_data=None
):
    registros = conexao.execute("""
        SELECT
            rendimento,
            data_credito
        FROM rendimentos_diarios
        WHERE substr(data_credito, 4, 2) = ?
          AND substr(data_credito, 7, 4) = ?
    """, (
        mes,
        ano
    )).fetchall()

    total = 0.0

    for item in registros:
        data_credito = parse_data_banco(
            item["data_credito"]
        )

        if (
            ate_data is not None
            and data_credito is not None
            and data_credito > ate_data
        ):
            continue

        total += float(
            item["rendimento"]
            or 0
        )

    return total


def soma_rendimentos_disponiveis_periodo(
    conexao,
    mes,
    ano
):
    hoje = datetime.now().date()
    periodo = (
        int(ano),
        int(mes)
    )
    periodo_atual = (
        hoje.year,
        hoje.month
    )

    if periodo > periodo_atual:
        return 0.0

    if periodo == periodo_atual:
        return soma_rendimentos_mes(
            conexao,
            mes,
            ano,
            ate_data=hoje
        )

    return soma_rendimentos_mes(
        conexao,
        mes,
        ano
    )


def total_receitas_ate_data(
    conexao,
    data_limite,
    somente_elegiveis=False
):
    mes = f"{data_limite.month:02d}"
    ano = str(
        data_limite.year
    )

    registros = conexao.execute("""
        SELECT
            valor,
            data
        FROM receitas
        WHERE substr(data, 4, 2) = ?
          AND substr(data, 7, 4) = ?
    """, (
        mes,
        ano
    )).fetchall()

    total = 0.0

    for item in registros:
        data_item = parse_data_banco(
            item["data"]
        )

        if not data_item:
            continue

        if somente_elegiveis:
            data_elegivel = (
                data_receita_fica_elegivel(
                    item["data"]
                )
            )

            if (
                data_elegivel
                and data_elegivel <= data_limite
            ):
                total += float(
                    item["valor"]
                    or 0
                )

        elif data_item <= data_limite:
            total += float(
                item["valor"]
                or 0
            )

    return total


def total_despesas_pagas_ate_data(
    conexao,
    data_limite
):
    mes = f"{data_limite.month:02d}"
    ano = str(
        data_limite.year
    )

    registros = conexao.execute("""
        SELECT
            valor,
            data,
            data_pagamento,
            COALESCE(pago, 1) AS pago
        FROM despesas
        WHERE substr(data, 4, 2) = ?
          AND substr(data, 7, 4) = ?
          AND COALESCE(pago, 1) = 1
    """, (
        mes,
        ano
    )).fetchall()

    total = 0.0

    for item in registros:
        data_efetiva = (
            parse_data_banco(
                item["data_pagamento"]
            )
            or parse_data_banco(
                item["data"]
            )
        )

        if (
            data_efetiva
            and data_efetiva <= data_limite
        ):
            total += float(
                item["valor"]
                or 0
            )

    return total


def saldo_fechamento_para_rendimento(
    conexao,
    data_referencia
):
    mes = f"{data_referencia.month:02d}"
    ano = str(
        data_referencia.year
    )

    gerar_despesas_fixas_do_periodo(
        conexao,
        mes,
        ano
    )

    saldo_anterior = (
        calcular_saldo_mes_anterior(
            conexao,
            mes,
            ano
        )
    )

    receitas = (
        total_receitas_ate_data(
            conexao,
            data_referencia,
            somente_elegiveis=True
        )
    )

    despesas_pagas = (
        total_despesas_pagas_ate_data(
            conexao,
            data_referencia
        )
    )

    rendimentos_creditados = (
        soma_rendimentos_mes(
            conexao,
            mes,
            ano,
            ate_data=data_referencia
        )
    )

    return (
        saldo_anterior
        + receitas
        + rendimentos_creditados
        - despesas_pagas
    )


def processar_rendimentos_pendentes():
    if not DB_PATH.exists():
        return

    with conectar() as conexao:
        config = obter_config_rendimento(
            conexao
        )

        if (
            not config
            or int(config["ativo"] or 0) != 1
        ):
            return

        data_inicio = parse_data_banco(
            config["data_inicio"]
        )

        processar_a_partir = (
            parse_data_banco(
                config["processar_a_partir_de"]
            )
            or data_inicio
        )

        if not data_inicio:
            return

        ultima = conexao.execute("""
            SELECT data_referencia
            FROM rendimentos_diarios
            ORDER BY
                substr(data_referencia, 7, 4) DESC,
                substr(data_referencia, 4, 2) DESC,
                substr(data_referencia, 1, 2) DESC,
                id DESC
            LIMIT 1
        """).fetchone()

        if ultima:
            ultima_data = parse_data_banco(
                ultima["data_referencia"]
            )

            inicio_processamento = (
                ultima_data
                + timedelta(days=1)
            )
        else:
            inicio_processamento = (
                data_inicio
            )

        if (
            processar_a_partir
            and inicio_processamento < processar_a_partir
        ):
            inicio_processamento = (
                processar_a_partir
            )

        fim_processamento = (
            datetime.now().date()
            - timedelta(days=1)
        )

        if inicio_processamento > fim_processamento:
            return

        taxa_diaria = taxa_diaria_rendimento(
            config["taxa_anual"],
            config["percentual_cdi"]
        )

        data_atual = (
            inicio_processamento
        )

        while data_atual <= fim_processamento:

            if eh_dia_util_rendimento(
                data_atual
            ):
                ja_existe = conexao.execute("""
                    SELECT 1
                    FROM rendimentos_diarios
                    WHERE data_referencia = ?
                """, (
                    formatar_data_banco(
                        data_atual
                    ),
                )).fetchone()

                if not ja_existe:
                    saldo_base = (
                        saldo_fechamento_para_rendimento(
                            conexao,
                            data_atual
                        )
                    )

                    saldo_elegivel = max(
                        saldo_base,
                        0
                    )

                    rendimento_bruto = (
                        saldo_elegivel
                        * taxa_diaria
                    )

                    aliquota_ir = (
                        aliquota_ir_regressiva(
                            config["data_inicio"],
                            data_atual
                        )
                        if int(
                            config.get(
                                "aplicar_ir",
                                1
                            )
                            or 0
                        ) == 1
                        else 0.0
                    )

                    imposto_ir = (
                        rendimento_bruto
                        * (
                            aliquota_ir
                            / 100
                        )
                    )

                    rendimento = (
                        rendimento_bruto
                        - imposto_ir
                    )

                    data_credito = (
                        proximo_dia_util(
                            data_atual
                        )
                    )

                    conexao.execute("""
                        INSERT INTO rendimentos_diarios (
                            data_referencia,
                            data_credito,
                            saldo_base,
                            taxa_anual,
                            percentual_cdi,
                            taxa_diaria,
                            rendimento,
                            criado_em,
                            rendimento_bruto,
                            aliquota_ir,
                            imposto_ir,
                            saldo_elegivel
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        formatar_data_banco(
                            data_atual
                        ),
                        formatar_data_banco(
                            data_credito
                        ),
                        saldo_base,
                        float(
                            config["taxa_anual"]
                            or 0
                        ),
                        float(
                            config["percentual_cdi"]
                            or 0
                        ),
                        taxa_diaria,
                        rendimento,
                        datetime.now().strftime(
                            "%d/%m/%Y %H:%M:%S"
                        ),
                        rendimento_bruto,
                        aliquota_ir,
                        imposto_ir,
                        saldo_elegivel
                    ))

                    conexao.commit()

            data_atual += timedelta(
                days=1
            )


def obter_ultimo_rendimento_creditado(
    conexao
):
    hoje = datetime.now().date()

    registros = conexao.execute("""
        SELECT
            data_referencia,
            data_credito,
            saldo_base,
            taxa_anual,
            percentual_cdi,
            taxa_diaria,
            rendimento
        FROM rendimentos_diarios
        ORDER BY
            substr(data_credito, 7, 4) DESC,
            substr(data_credito, 4, 2) DESC,
            substr(data_credito, 1, 2) DESC,
            id DESC
    """).fetchall()

    for item in registros:
        credito = parse_data_banco(
            item["data_credito"]
        )

        if (
            credito
            and credito <= hoje
        ):
            return dict(
                item
            )

    return None


def estimar_proximo_rendimento(
    saldo_atual,
    config
):
    if (
        not config
        or int(config["ativo"] or 0) != 1
    ):
        return {
            "valor": 0.0,
            "valor_bruto": 0.0,
            "imposto_ir": 0.0,
            "aliquota_ir": 0.0,
            "data_referencia": None,
            "data_credito": None,
            "taxa_diaria": 0.0,
        }

    hoje = datetime.now().date()

    if eh_dia_util_rendimento(
        hoje
    ):
        data_referencia = hoje
    else:
        data_referencia = (
            proximo_dia_util(
                hoje
            )
        )

    data_credito = (
        proximo_dia_util(
            data_referencia
        )
    )

    taxa_diaria = (
        taxa_diaria_rendimento(
            config["taxa_anual"],
            config["percentual_cdi"]
        )
    )

    rendimento_bruto = (
        max(
            float(saldo_atual or 0),
            0
        )
        * taxa_diaria
    )

    aliquota_ir = (
        aliquota_ir_regressiva(
            config["data_inicio"],
            data_referencia
        )
        if int(
            config.get(
                "aplicar_ir",
                1
            )
            or 0
        ) == 1
        else 0.0
    )

    imposto_ir = (
        rendimento_bruto
        * (
            aliquota_ir
            / 100
        )
    )

    return {
        "valor": (
            rendimento_bruto
            - imposto_ir
        ),
        "valor_bruto": rendimento_bruto,
        "imposto_ir": imposto_ir,
        "aliquota_ir": aliquota_ir,
        "data_referencia": (
            formatar_data_banco(
                data_referencia
            )
        ),
        "data_credito": (
            formatar_data_banco(
                data_credito
            )
        ),
        "taxa_diaria": taxa_diaria,
    }

def obter_nomes_categorias(tipo):
    with conectar() as conexao:
        registros = conexao.execute("""
            SELECT nome
            FROM categorias
            WHERE tipo = ?
            ORDER BY nome COLLATE NOCASE
        """, (tipo,)).fetchall()

    return [
        item["nome"]
        for item in registros
    ]


def contar_uso_categoria(conexao, tipo, nome):
    if tipo == "despesa":
        uso_despesas = conexao.execute("""
            SELECT COUNT(*)
            FROM despesas
            WHERE categoria = ? COLLATE NOCASE
        """, (nome,)).fetchone()[0]

        uso_recorrencias = conexao.execute("""
            SELECT COUNT(*)
            FROM despesas_fixas
            WHERE categoria = ? COLLATE NOCASE
        """, (nome,)).fetchone()[0]

        uso_servicos_cartao = conexao.execute("""
            SELECT COUNT(*)
            FROM cartao_servicos
            WHERE categoria = ? COLLATE NOCASE
        """, (nome,)).fetchone()[0]

        return int(uso_despesas or 0) + int(
            uso_recorrencias or 0
        ) + int(uso_servicos_cartao or 0)

    uso_receitas = conexao.execute("""
        SELECT COUNT(*)
        FROM receitas
        WHERE categoria = ? COLLATE NOCASE
    """, (nome,)).fetchone()[0]

    return int(uso_receitas or 0)


def listar_categorias_com_uso(conexao, tipo):
    registros = conexao.execute("""
        SELECT
            id,
            nome,
            tipo
        FROM categorias
        WHERE tipo = ?
        ORDER BY nome COLLATE NOCASE
    """, (tipo,)).fetchall()

    resultado = []

    for item in registros:
        categoria = dict(item)
        categoria["uso"] = contar_uso_categoria(
            conexao,
            tipo,
            categoria["nome"]
        )
        resultado.append(categoria)

    return resultado


def atualizar_categoria_nos_lancamentos(
    conexao,
    tipo,
    categoria_antiga,
    categoria_nova
):
    if tipo == "despesa":
        conexao.execute("""
            UPDATE despesas
            SET categoria = ?
            WHERE categoria = ? COLLATE NOCASE
        """, (
            categoria_nova,
            categoria_antiga
        ))

        conexao.execute("""
            UPDATE despesas_fixas
            SET categoria = ?
            WHERE categoria = ? COLLATE NOCASE
        """, (
            categoria_nova,
            categoria_antiga
        ))

        conexao.execute("""
            UPDATE cartao_servicos
            SET categoria = ?
            WHERE categoria = ? COLLATE NOCASE
        """, (
            categoria_nova,
            categoria_antiga
        ))

    else:
        conexao.execute("""
            UPDATE receitas
            SET categoria = ?
            WHERE categoria = ? COLLATE NOCASE
        """, (
            categoria_nova,
            categoria_antiga
        ))


def valor_csv(valor):
    try:
        numero = float(valor or 0)
    except (TypeError, ValueError):
        numero = 0

    return f"{numero:.2f}".replace(".", ",")


def criar_csv(cabecalhos, linhas):
    buffer = io.StringIO(
        newline=""
    )

    escritor = csv.writer(
        buffer,
        delimiter=";",
        quotechar='"',
        quoting=csv.QUOTE_MINIMAL,
        lineterminator="\r\n"
    )

    escritor.writerow(
        cabecalhos
    )

    for linha in linhas:
        escritor.writerow(
            linha
        )

    # BOM UTF-8 ajuda Excel/Windows a reconhecer acentos corretamente.
    conteudo = "\ufeff" + buffer.getvalue()

    return conteudo.encode(
        "utf-8"
    )


def resposta_csv(nome_arquivo, cabecalhos, linhas):
    conteudo = criar_csv(
        cabecalhos,
        linhas
    )

    return Response(
        conteudo,
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{nome_arquivo}"'
            )
        }
    )


def adicionar_csv_ao_zip(
    arquivo_zip,
    nome_arquivo,
    cabecalhos,
    linhas
):
    arquivo_zip.writestr(
        nome_arquivo,
        criar_csv(
            cabecalhos,
            linhas
        )
    )


def linhas_despesas_exportacao(itens):
    return [
        [
            item["id"],
            item["descricao"],
            item["categoria"],
            valor_csv(
                item["valor"]
            ),
            item["data"] or "",
            item["status"],
            item["data_pagamento"] or "",
            item["origem"],
        ]
        for item in itens
    ]


def linhas_receitas_exportacao(itens):
    return [
        [
            item["id"],
            item["descricao"],
            item["categoria"],
            valor_csv(
                item["valor"]
            ),
            item["data"] or "",
        ]
        for item in itens
    ]

def obter_periodo():
    hoje = datetime.now()

    # Ao abrir o aplicativo diretamente (sem mes/ano na URL),
    # o período inicial deve ser sempre o mês/ano corrente.
    # Isso evita que uma sessão antiga faça o Controle 2 abrir
    # em um mês anterior e, consequentemente, esconda cobranças
    # recorrentes que já chegaram à data de cobrança.
    mes_recebido = request.args.get("mes")
    ano_recebido = request.args.get("ano")
    abertura_direta = not mes_recebido and not ano_recebido

    if abertura_direta and request.endpoint in {"dashboard", "cartao"}:
        session["mes_selecionado"] = hoje.strftime("%m")
        session["ano_selecionado"] = str(hoje.year)

    if mes_recebido in MESES:
        mes = mes_recebido
        session["mes_selecionado"] = mes
    else:
        mes = session.get(
            "mes_selecionado",
            hoje.strftime("%m")
        )

    if (
        ano_recebido
        and ano_recebido.isdigit()
        and len(ano_recebido) == 4
    ):
        ano = ano_recebido
        session["ano_selecionado"] = ano
    else:
        ano = session.get(
            "ano_selecionado",
            str(hoje.year)
        )

    # Proteção caso exista algum valor antigo/inválido
    # salvo no cookie de sessão.
    if mes not in MESES:
        mes = hoje.strftime("%m")
        session["mes_selecionado"] = mes

    if not str(ano).isdigit() or len(str(ano)) != 4:
        ano = str(hoje.year)
        session["ano_selecionado"] = ano

    return mes, str(ano)


def obter_anos(ano_central):
    centro = int(ano_central)
    return [str(a) for a in range(centro - 5, centro + 6)]


def soma(conexao, tabela, mes, ano, extra_where=""):
    consulta = f"""
        SELECT COALESCE(SUM(valor), 0)
        FROM {tabela}
        WHERE substr(data, 4, 2) = ?
          AND substr(data, 7, 4) = ?
    """

    parametros = [mes, ano]

    if extra_where:
        consulta += " AND " + extra_where

    return float(conexao.execute(consulta, parametros).fetchone()[0] or 0)


def calcular_saldo_mes_anterior(conexao, mes, ano):
    """Retorna o saldo atual do mês imediatamente anterior, acumulado.

    O saldo atual de cada mês é carregado integralmente para o mês
    seguinte. Assim, o cálculo não subtrai o saldo de um mês do outro;
    ele usa o saldo anterior como ponto de partida e soma/subtrai apenas
    os movimentos do mês em análise.
    """
    mes_int = int(mes)
    ano_int = int(ano)

    if mes_int == 1:
        mes_anterior = 12
        ano_anterior = ano_int - 1
    else:
        mes_anterior = mes_int - 1
        ano_anterior = ano_int

    mes_anterior_txt = f"{mes_anterior:02d}"
    ano_anterior_txt = str(ano_anterior)

    receitas = float(conexao.execute("""
        SELECT COALESCE(SUM(valor), 0)
        FROM receitas
        WHERE (
            substr(data, 7, 4) < ?
            OR (substr(data, 7, 4) = ? AND substr(data, 4, 2) <= ?)
        )
    """, (ano_anterior_txt, ano_anterior_txt, mes_anterior_txt)).fetchone()[0] or 0)

    despesas_pagas = float(conexao.execute("""
        SELECT COALESCE(SUM(valor), 0)
        FROM despesas
        WHERE COALESCE(pago, 1) = 1
          AND (
            substr(data, 7, 4) < ?
            OR (substr(data, 7, 4) = ? AND substr(data, 4, 2) <= ?)
          )
    """, (ano_anterior_txt, ano_anterior_txt, mes_anterior_txt)).fetchone()[0] or 0)

    rendimentos = float(conexao.execute("""
        SELECT COALESCE(SUM(rendimento), 0)
        FROM rendimentos_diarios
        WHERE (
            substr(data_credito, 7, 4) < ?
            OR (substr(data_credito, 7, 4) = ? AND substr(data_credito, 4, 2) <= ?)
        )
    """, (ano_anterior_txt, ano_anterior_txt, mes_anterior_txt)).fetchone()[0] or 0)

    return receitas + rendimentos - despesas_pagas



def listar_cartoes(conexao):
    registros = conexao.execute("""
        SELECT
            id,
            nome,
            dia_fechamento,
            dia_vencimento,
            limite,
            ativo,
            criado_em
        FROM cartoes
        ORDER BY ativo DESC, nome COLLATE NOCASE, id
    """).fetchall()

    return [dict(registro) for registro in registros]


def obter_config_cartao(conexao, cartao_id=None):
    if cartao_id is None:
        registro = conexao.execute("""
            SELECT
                id,
                nome,
                dia_fechamento,
                dia_vencimento,
                limite,
                ativo,
                criado_em
            FROM cartoes
            ORDER BY ativo DESC, id
            LIMIT 1
        """).fetchone()
    else:
        registro = conexao.execute("""
            SELECT
                id,
                nome,
                dia_fechamento,
                dia_vencimento,
                limite,
                ativo,
                criado_em
            FROM cartoes
            WHERE id = ?
        """, (cartao_id,)).fetchone()

    return dict(registro) if registro else None


def proximo_mes_competencia(ano, mes):
    ano = int(ano)
    mes = int(mes)
    if mes == 12:
        return ano + 1, 1
    return ano, mes + 1


def avancar_competencia_cartao(ano, mes, quantidade):
    """Avança N meses a partir de uma competência YYYY/MM."""
    ano = int(ano)
    mes = int(mes)
    quantidade = int(quantidade)

    total_meses = (ano * 12) + (mes - 1) + quantidade
    novo_ano = total_meses // 12
    novo_mes = (total_meses % 12) + 1
    return novo_ano, novo_mes


def converter_valor_cartao(valor_texto):
    """Aceita formatos como 1200,00, 1.200,00 e 1200.00."""
    texto = str(valor_texto or "").strip()
    texto = texto.replace("R$", "").replace(" ", "")

    if not texto:
        raise ValueError

    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    elif texto.count(".") > 1:
        texto = texto.replace(".", "")

    return float(texto)


def valores_parcelas_cartao(valor_total, quantidade):
    """Divide o total em centavos, deixando eventual ajuste de centavos na última parcela."""
    quantidade = int(quantidade)
    total_centavos = int(round(float(valor_total) * 100))
    base = total_centavos // quantidade
    resto = total_centavos % quantidade

    valores = []
    for indice in range(1, quantidade + 1):
        centavos = base
        if indice == quantidade:
            centavos += resto
        valores.append(centavos / 100)

    return valores


def calcular_competencias_parcelas_cartao(data_compra, dia_fechamento, quantidade):
    primeiro_ano, primeiro_mes = calcular_competencia_cartao(
        data_compra,
        dia_fechamento
    )

    return [
        avancar_competencia_cartao(
            primeiro_ano,
            primeiro_mes,
            indice
        )
        for indice in range(int(quantidade))
    ]


@lru_cache(maxsize=16)
def feriados_nacionais_brasil(ano):
    """Carrega os feriados nacionais brasileiros do ano informado.

    A biblioteca `holidays` calcula automaticamente os feriados móveis,
    como a Paixão de Cristo, e mantém a lista legal atualizada.
    Pontos facultativos não são considerados feriados.
    """
    return holidays.country_holidays("BR", years=[int(ano)])


def eh_dia_util_brasil(data):
    """Retorna True somente para segunda a sexta que não seja feriado nacional."""
    if data.weekday() >= 5:
        return False

    return data not in feriados_nacionais_brasil(data.year)


def proximo_dia_util(data):
    """Avança até o próximo dia útil, incluindo a própria data se ela for útil."""
    data = data
    while not eh_dia_util_brasil(data):
        data += timedelta(days=1)
    return data


def data_fechamento_cartao(ano, mes, dia_fechamento):
    """Calcula a data efetiva do fechamento, empurrando para o próximo dia útil."""
    ano = int(ano)
    mes = int(mes)
    ultimo_dia = calendar.monthrange(ano, mes)[1]
    dia_real = min(int(dia_fechamento), ultimo_dia)
    data_base = datetime(ano, mes, dia_real).date()
    return proximo_dia_util(data_base)


def calcular_competencia_cartao(data_compra, dia_fechamento):
    """A compra até o fechamento efetivo entra na fatura daquela competência.

    Se o fechamento fixado cair em sábado, domingo ou feriado, ele passa para
    o próximo dia útil. A comparação usa a data efetiva, inclusive quando
    esse ajuste ultrapassa o último dia do mês.
    """
    fechamento = data_fechamento_cartao(
        data_compra.year,
        data_compra.month,
        dia_fechamento
    )

    if data_compra <= fechamento:
        return data_compra.year, data_compra.month

    return proximo_mes_competencia(
        data_compra.year,
        data_compra.month
    )


def calcular_datas_fatura(ano, mes, dia_fechamento, dia_vencimento):
    ano = int(ano)
    mes = int(mes)

    data_fechamento = data_fechamento_cartao(
        ano,
        mes,
        dia_fechamento
    )

    ano_vencimento, mes_vencimento = proximo_mes_competencia(
        ano,
        mes
    )

    ultimo_dia_vencimento = calendar.monthrange(
        ano_vencimento,
        mes_vencimento
    )[1]

    dia_vencimento_real = min(
        int(dia_vencimento),
        ultimo_dia_vencimento
    )

    data_vencimento_base = datetime(
        ano_vencimento,
        mes_vencimento,
        dia_vencimento_real
    ).date()
    data_vencimento = proximo_dia_util(data_vencimento_base)

    return (
        data_fechamento.strftime("%d/%m/%Y"),
        data_vencimento.strftime("%d/%m/%Y")
    )


def obter_ou_criar_fatura_cartao(conexao, ano, mes, config):
    competencia = f"{int(ano):04d}-{int(mes):02d}"
    cartao_id = int(config["id"])

    existente = conexao.execute("""
        SELECT *
        FROM cartao_faturas
        WHERE cartao_id = ?
          AND competencia = ?
    """, (cartao_id, competencia)).fetchone()

    if existente:
        return existente

    data_fechamento, data_vencimento = calcular_datas_fatura(
        ano,
        mes,
        config["dia_fechamento"],
        config["dia_vencimento"]
    )

    conexao.execute("""
        INSERT INTO cartao_faturas (
            cartao_id,
            competencia,
            data_fechamento,
            data_vencimento,
            pago,
            data_pagamento,
            despesa_id,
            criado_em
        )
        VALUES (?, ?, ?, ?, 0, NULL, NULL, ?)
    """, (
        cartao_id,
        competencia,
        data_fechamento,
        data_vencimento,
        datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    ))

    return conexao.execute("""
        SELECT *
        FROM cartao_faturas
        WHERE cartao_id = ?
          AND competencia = ?
    """, (cartao_id, competencia)).fetchone()


def garantir_categoria_cartao(conexao):
    conexao.execute("""
        INSERT OR IGNORE INTO categorias (
            nome,
            tipo
        )
        VALUES ('Cartão de Crédito', 'despesa')
    """)


def listar_servicos_cartao(conexao, cartao_id):
    registros = conexao.execute("""
        SELECT
            id,
            cartao_id,
            descricao,
            valor,
            categoria,
            data_inicio,
            ativo,
            criado_em
        FROM cartao_servicos
        WHERE cartao_id = ?
          AND ativo = 1
        ORDER BY
            substr(data_inicio, 7, 4) ASC,
            substr(data_inicio, 4, 2) ASC,
            substr(data_inicio, 1, 2) ASC,
            id ASC
    """, (cartao_id,)).fetchall()

    return [dict(registro) for registro in registros]


def competencia_para_data(ano, mes):
    return datetime(int(ano), int(mes), 1).date()


def sincronizar_servicos_cartao(conexao):
    """Lança cobranças recorrentes somente quando chega a data de cobrança.

    A primeira cobrança respeita exatamente ``data_inicio``. Depois disso,
    cada mês é gerado na mesma data (ajustada ao último dia do mês quando
    necessário) e só é materializado na fatura quando a data da cobrança
    chega. A fatura é determinada pelo fechamento do cartão.

    Isso evita projetar serviços em 12 meses de faturas futuras. Faturas já
    pagas permanecem como histórico.
    """
    hoje = datetime.now().date()

    servicos = conexao.execute("""
        SELECT
            s.id,
            s.cartao_id,
            s.descricao,
            s.valor,
            s.categoria,
            s.data_inicio,
            c.dia_fechamento
        FROM cartao_servicos s
        JOIN cartoes c
            ON c.id = s.cartao_id
        WHERE s.ativo = 1
        ORDER BY s.id
    """).fetchall()

    for servico in servicos:
        data_inicio = parse_data_banco(servico["data_inicio"])
        if not data_inicio or data_inicio > hoje:
            continue

        config = obter_config_cartao(conexao, servico["cartao_id"])
        if not config:
            continue

        # Remove apenas projeções futuras não pagas. O histórico pago nunca
        # é alterado.
        conexao.execute("""
            DELETE FROM cartao_servicos_faturas
            WHERE servico_id = ?
              AND fatura_id IN (
                  SELECT id FROM cartao_faturas WHERE pago = 0
              )
        """, (servico["id"],))

        cursor = data_inicio
        while cursor <= hoje:
            # A cobrança recorrente pertence ao mês da sua data de cobrança.
            # Ex.: primeira cobrança em 12/10/2026 -> fatura de outubro.
            # O fechamento continua valendo para compras normais do cartão,
            # mas não desloca a competência de uma cobrança recorrente.
            ano_fatura, mes_fatura = cursor.year, cursor.month

            fatura = obter_ou_criar_fatura_cartao(
                conexao,
                ano_fatura,
                mes_fatura,
                config
            )

            if not int(fatura["pago"] or 0):
                conexao.execute("""
                    INSERT OR IGNORE INTO cartao_servicos_faturas (
                        servico_id,
                        fatura_id,
                        descricao,
                        valor,
                        categoria,
                        criado_em
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    servico["id"],
                    fatura["id"],
                    servico["descricao"],
                    float(servico["valor"]),
                    servico["categoria"],
                    datetime.now().strftime("%d/%m/%Y %H:%M:%S")
                ))

            proximo_ano, proximo_mes = avancar_competencia_cartao(
                cursor.year, cursor.month, 1
            )
            ultimo_dia = calendar.monthrange(proximo_ano, proximo_mes)[1]
            dia = min(data_inicio.day, ultimo_dia)
            cursor = datetime(proximo_ano, proximo_mes, dia).date()


def garantir_servicos_da_fatura_atual(conexao, fatura_id):
    """Garante que os serviços cuja cobrança já chegou apareçam na fatura.

    Esta verificação é feita também no momento de abrir a aba Cartão. Assim,
    uma cobrança que atingiu a data de cobrança não depende apenas da rotina
    global de sincronização executada no início da requisição.
    """
    fatura = conexao.execute("""
        SELECT id, cartao_id, competencia, pago
        FROM cartao_faturas
        WHERE id = ?
    """, (fatura_id,)).fetchone()

    if not fatura or int(fatura["pago"] or 0):
        return

    try:
        ano, mes = map(int, str(fatura["competencia"]).split("-"))
    except (TypeError, ValueError):
        return

    hoje = datetime.now().date()
    servicos = conexao.execute("""
        SELECT id, descricao, valor, categoria, data_inicio
        FROM cartao_servicos
        WHERE cartao_id = ?
          AND ativo = 1
        ORDER BY id
    """, (fatura["cartao_id"],)).fetchall()

    for servico in servicos:
        data_inicio = parse_data_banco(servico["data_inicio"])
        if not data_inicio:
            continue

        # O serviço só passa a existir a partir do mês da primeira cobrança.
        if (ano, mes) < (data_inicio.year, data_inicio.month):
            continue

        ultimo_dia = calendar.monthrange(ano, mes)[1]
        dia_cobranca = min(data_inicio.day, ultimo_dia)
        data_cobranca = datetime(ano, mes, dia_cobranca).date()

        # A cobrança só entra quando sua data realmente chegou.
        if data_cobranca > hoje:
            continue

        conexao.execute("""
            INSERT OR IGNORE INTO cartao_servicos_faturas (
                servico_id,
                fatura_id,
                descricao,
                valor,
                categoria,
                criado_em
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            servico["id"],
            fatura_id,
            servico["descricao"],
            float(servico["valor"]),
            servico["categoria"],
            datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        ))


def servicos_da_fatura(conexao, fatura_id):
    registros = conexao.execute("""
        SELECT
            sf.id,
            sf.servico_id,
            sf.fatura_id,
            sf.descricao,
            sf.valor,
            sf.categoria,
            s.data_inicio,
            f.competencia
        FROM cartao_servicos_faturas sf
        LEFT JOIN cartao_servicos s
            ON s.id = sf.servico_id
        JOIN cartao_faturas f
            ON f.id = sf.fatura_id
        WHERE sf.fatura_id = ?
        ORDER BY sf.descricao COLLATE NOCASE, sf.id
    """, (fatura_id,)).fetchall()

    resultado = []
    for registro in registros:
        item = dict(registro)
        data_inicio = parse_data_banco(item.get("data_inicio"))
        data_cobranca = None
        if data_inicio:
            try:
                ano, mes = map(int, str(item["competencia"]).split("-"))
                ultimo_dia = calendar.monthrange(ano, mes)[1]
                dia = min(data_inicio.day, ultimo_dia)
                data_cobranca = datetime(ano, mes, dia).date().strftime("%d/%m/%Y")
            except (TypeError, ValueError):
                data_cobranca = None
        item["data_cobranca"] = data_cobranca
        resultado.append(item)

    return resultado


def sincronizar_fatura_cartao(conexao, fatura_id):
    fatura = conexao.execute("""
        SELECT *
        FROM cartao_faturas
        WHERE id = ?
    """, (fatura_id,)).fetchone()

    if not fatura:
        return

    total_compras = float(conexao.execute("""
        SELECT COALESCE(SUM(valor), 0)
        FROM cartao_parcelas
        WHERE fatura_id = ?
    """, (fatura_id,)).fetchone()[0] or 0)

    total_servicos = float(conexao.execute("""
        SELECT COALESCE(SUM(valor), 0)
        FROM cartao_servicos_faturas
        WHERE fatura_id = ?
    """, (fatura_id,)).fetchone()[0] or 0)

    total = total_compras + total_servicos

    quantidade_compras = int(conexao.execute("""
        SELECT COUNT(DISTINCT compra_id)
        FROM cartao_parcelas
        WHERE fatura_id = ?
    """, (fatura_id,)).fetchone()[0] or 0)

    quantidade_servicos = int(conexao.execute("""
        SELECT COUNT(*)
        FROM cartao_servicos_faturas
        WHERE fatura_id = ?
    """, (fatura_id,)).fetchone()[0] or 0)

    quantidade = quantidade_compras + quantidade_servicos

    if quantidade == 0:
        if fatura["despesa_id"]:
            conexao.execute("""
                DELETE FROM despesas
                WHERE id = ?
                  AND cartao_fatura_id = ?
            """, (fatura["despesa_id"], fatura_id))

        conexao.execute("""
            DELETE FROM cartao_faturas
            WHERE id = ?
              AND pago = 0
        """, (fatura_id,))
        return

    config = obter_config_cartao(conexao, fatura["cartao_id"])
    nome_cartao = (
        config["nome"]
        if config and config["nome"]
        else "Meu cartão"
    )

    descricao = f"Fatura {nome_cartao} - {fatura['competencia'][5:]}/{fatura['competencia'][:4]}"

    garantir_categoria_cartao(conexao)

    pago = int(fatura["pago"] or 0)
    data_pagamento = fatura["data_pagamento"] if pago else None

    if fatura["despesa_id"]:
        despesa = conexao.execute("""
            SELECT id
            FROM despesas
            WHERE id = ?
        """, (fatura["despesa_id"],)).fetchone()

        if despesa:
            conexao.execute("""
                UPDATE despesas
                SET descricao = ?,
                    valor = ?,
                    categoria = 'Cartão de Crédito',
                    data = ?,
                    pago = ?,
                    data_pagamento = ?,
                    despesa_fixa_id = NULL,
                    cartao_fatura_id = ?
                WHERE id = ?
            """, (
                descricao,
                total,
                fatura["data_vencimento"],
                pago,
                data_pagamento,
                fatura_id,
                fatura["despesa_id"]
            ))
            return

    existente = conexao.execute("""
        SELECT id
        FROM despesas
        WHERE cartao_fatura_id = ?
        LIMIT 1
    """, (fatura_id,)).fetchone()

    if existente:
        despesa_id = existente["id"]

        conexao.execute("""
            UPDATE despesas
            SET descricao = ?,
                valor = ?,
                categoria = 'Cartão de Crédito',
                data = ?,
                pago = ?,
                data_pagamento = ?,
                despesa_fixa_id = NULL
            WHERE id = ?
        """, (
            descricao,
            total,
            fatura["data_vencimento"],
            pago,
            data_pagamento,
            despesa_id
        ))

        conexao.execute("""
            UPDATE cartao_faturas
            SET despesa_id = ?
            WHERE id = ?
        """, (despesa_id, fatura_id))
        return

    cursor = conexao.execute("""
        INSERT INTO despesas (
            descricao,
            valor,
            categoria,
            data,
            pago,
            data_pagamento,
            despesa_fixa_id,
            cartao_fatura_id
        )
        VALUES (?, ?, 'Cartão de Crédito', ?, ?, ?, NULL, ?)
    """, (
        descricao,
        total,
        fatura["data_vencimento"],
        pago,
        data_pagamento,
        fatura_id
    ))

    conexao.execute("""
        UPDATE cartao_faturas
        SET despesa_id = ?
        WHERE id = ?
    """, (cursor.lastrowid, fatura_id))


def atualizar_datas_faturas_cartao(conexao):
    """Atualiza datas de faturas ainda não pagas para o calendário útil atual.

    Faturas pagas permanecem históricas e não são alteradas.
    """
    faturas = conexao.execute("""
        SELECT
            f.id,
            f.competencia,
            f.pago,
            c.dia_fechamento,
            c.dia_vencimento
        FROM cartao_faturas f
        JOIN cartoes c
            ON c.id = f.cartao_id
        WHERE COALESCE(f.pago, 0) = 0
    """).fetchall()

    for fatura in faturas:
        try:
            ano = int(fatura["competencia"][:4])
            mes = int(fatura["competencia"][5:7])
        except (TypeError, ValueError):
            continue

        data_fechamento, data_vencimento = calcular_datas_fatura(
            ano,
            mes,
            fatura["dia_fechamento"],
            fatura["dia_vencimento"]
        )

        conexao.execute("""
            UPDATE cartao_faturas
            SET data_fechamento = ?,
                data_vencimento = ?
            WHERE id = ?
        """, (data_fechamento, data_vencimento, fatura["id"]))


def sincronizar_faturas_cartao():
    if not DB_PATH.exists():
        return

    garantir_estrutura()

    with conectar() as conexao:
        atualizar_datas_faturas_cartao(conexao)
        sincronizar_servicos_cartao(conexao)

        faturas = conexao.execute("""
            SELECT id
            FROM cartao_faturas
            ORDER BY id
        """).fetchall()

        for fatura in faturas:
            sincronizar_fatura_cartao(
                conexao,
                fatura["id"]
            )

        conexao.commit()


def status_fatura_cartao(fatura):
    if int(fatura["pago"] or 0) == 1:
        return "Paga"

    data = parse_data_banco(fatura["data_vencimento"])

    if data and data < datetime.now().date():
        return "Atrasada"

    return "Pendente"


def resumo_parcelas_fatura_cartao(conexao, fatura_id):
    """Resume as parcelas presentes em uma fatura para exibição.

    Quando há mais de uma compra parcelada, mostra as combinações
    distintas (ex.: 1/10 · 2/6). Compras à vista e serviços recorrentes
    recebem rótulos próprios.
    """
    registros = conexao.execute("""
        SELECT DISTINCT
            p.numero_parcela,
            c.parcelas
        FROM cartao_parcelas p
        JOIN cartao_compras c
            ON c.id = p.compra_id
        WHERE p.fatura_id = ?
        ORDER BY p.numero_parcela ASC, c.parcelas ASC
    """, (fatura_id,)).fetchall()

    etiquetas = []
    for registro in registros:
        numero = int(registro["numero_parcela"] or 1)
        total = int(registro["parcelas"] or 1)
        etiqueta = f"{numero}/{total}" if total > 1 else "À vista"
        if etiqueta not in etiquetas:
            etiquetas.append(etiqueta)

    tem_servico = bool(conexao.execute("""
        SELECT 1
        FROM cartao_servicos_faturas
        WHERE fatura_id = ?
        LIMIT 1
    """, (fatura_id,)).fetchone())

    if etiquetas:
        return " · ".join(etiquetas)
    if tem_servico:
        return "Mensal"
    return "—"


def resumo_cartao(conexao, cartao_id):
    faturas = conexao.execute("""
        SELECT
            f.id,
            f.cartao_id,
            f.competencia,
            f.data_fechamento,
            f.data_vencimento,
            f.pago,
            f.data_pagamento,
            COALESCE((
                SELECT SUM(p.valor)
                FROM cartao_parcelas p
                WHERE p.fatura_id = f.id
            ), 0) + COALESCE((
                SELECT SUM(sf.valor)
                FROM cartao_servicos_faturas sf
                WHERE sf.fatura_id = f.id
            ), 0) AS total,
            COALESCE((
                SELECT COUNT(DISTINCT p.compra_id)
                FROM cartao_parcelas p
                WHERE p.fatura_id = f.id
            ), 0) + COALESCE((
                SELECT COUNT(*)
                FROM cartao_servicos_faturas sf
                WHERE sf.fatura_id = f.id
            ), 0) AS quantidade
        FROM cartao_faturas f
        WHERE f.cartao_id = ?
        ORDER BY f.data_vencimento ASC, f.id ASC
    """, (cartao_id,)).fetchall()

    resultado = []

    for item in faturas:
        registro = dict(item)
        registro["total"] = float(registro["total"] or 0)
        registro["quantidade"] = int(registro["quantidade"] or 0)
        registro["status"] = status_fatura_cartao(registro)
        registro["parcela"] = resumo_parcelas_fatura_cartao(
            conexao,
            registro["id"]
        )
        resultado.append(registro)

    return resultado

def converter_data_para_banco(data_html):
    try:
        return datetime.strptime(data_html, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return None


def converter_data_para_html(data_banco):
    if not data_banco:
        return ""

    try:
        return datetime.strptime(data_banco, "%d/%m/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def status_despesa(item):
    if int(item["pago"] or 0) == 1:
        return "Pago"

    data = item["data"]

    if data:
        try:
            vencimento = datetime.strptime(data, "%d/%m/%Y").date()
            if vencimento < datetime.now().date():
                return "Atrasada"
        except ValueError:
            pass

    return "Pendente"


def gerar_despesas_fixas_do_periodo(conexao, mes, ano):
    cursor = conexao.cursor()

    fixas = cursor.execute("""
        SELECT
            id,
            descricao,
            valor,
            categoria,
            dia_vencimento,
            data_inicio
        FROM despesas_fixas
        WHERE ativa = 1
    """).fetchall()

    mes_int = int(mes)
    ano_int = int(ano)
    periodo_selecionado = ano_int * 100 + mes_int
    competencia = f"{ano_int:04d}-{mes_int:02d}"

    for fixa in fixas:
        if not fixa["data_inicio"]:
            continue

        try:
            inicio = datetime.strptime(
                fixa["data_inicio"],
                "%d/%m/%Y"
            )
        except ValueError:
            continue

        periodo_inicio = inicio.year * 100 + inicio.month

        if periodo_selecionado < periodo_inicio:
            continue

        excluida = cursor.execute("""
            SELECT 1
            FROM despesas_fixas_excecoes
            WHERE despesa_fixa_id = ?
              AND competencia = ?
        """, (fixa["id"], competencia)).fetchone()

        if excluida:
            continue

        existente = cursor.execute("""
            SELECT id
            FROM despesas
            WHERE despesa_fixa_id = ?
              AND substr(data, 4, 2) = ?
              AND substr(data, 7, 4) = ?
            LIMIT 1
        """, (fixa["id"], mes, ano)).fetchone()

        if existente:
            continue

        ultimo_dia = calendar.monthrange(ano_int, mes_int)[1]
        dia_real = min(int(fixa["dia_vencimento"]), ultimo_dia)

        data_vencimento = datetime(
            ano_int,
            mes_int,
            dia_real
        ).strftime("%d/%m/%Y")

        cursor.execute("""
            INSERT INTO despesas (
                descricao,
                valor,
                categoria,
                data,
                pago,
                data_pagamento,
                despesa_fixa_id
            )
            VALUES (?, ?, ?, ?, 0, NULL, ?)
        """, (
            fixa["descricao"],
            fixa["valor"],
            fixa["categoria"],
            data_vencimento,
            fixa["id"]
        ))

    conexao.commit()


def carregar_despesas(conexao, mes, ano, origem=None, ordem="asc"):
    consulta = """
        SELECT
            id,
            descricao,
            categoria,
            valor,
            data,
            COALESCE(pago, 1) AS pago,
            data_pagamento,
            despesa_fixa_id,
            cartao_fatura_id
        FROM despesas
        WHERE substr(data, 4, 2) = ?
          AND substr(data, 7, 4) = ?
    """

    parametros = [mes, ano]

    if origem == "gerais":
        consulta += " AND despesa_fixa_id IS NULL"

        # Despesas Gerais: ordem escolhida pelo usuário.
        if ordem == "asc":
            consulta += """
                ORDER BY
                    CAST(substr(data, 1, 2) AS INTEGER),
                    id
            """
        else:
            consulta += """
                ORDER BY
                    CAST(substr(data, 1, 2) AS INTEGER) DESC,
                    id DESC
            """

    elif origem == "fixas":
        consulta += " AND despesa_fixa_id IS NOT NULL"

        # Despesas Fixas: crescente por padrão, com opção de inverter.
        if ordem == "desc":
            consulta += """
                ORDER BY
                    CAST(substr(data, 1, 2) AS INTEGER) DESC,
                    id DESC
            """
        else:
            consulta += """
                ORDER BY
                    CAST(substr(data, 1, 2) AS INTEGER),
                    id
            """

    else:
        consulta += """
            ORDER BY
                CAST(substr(data, 1, 2) AS INTEGER),
                id
        """

    itens = conexao.execute(
        consulta,
        parametros
    ).fetchall()

    resultado = []

    for item in itens:
        registro = dict(item)
        registro["status"] = status_despesa(item)
        if item["cartao_fatura_id"] is not None:
            registro["origem"] = "Cartão"
        elif item["despesa_fixa_id"] is not None:
            registro["origem"] = "Fixa"
        else:
            registro["origem"] = "Manual"
        resultado.append(registro)

    return resultado


def redirecionar_pagina_despesas(pagina, mes, ano):
    endpoint = (
        "despesas_fixas"
        if pagina == "fixas"
        else "despesas"
    )

    return redirect(
        url_for(
            endpoint,
            mes=mes,
            ano=ano
        )
    )



def agrupar_por_categoria(
    conexao,
    tabela,
    mes,
    ano,
    extra_where=""
):
    consulta = f"""
        SELECT
            categoria,
            COALESCE(SUM(valor), 0) AS total
        FROM {tabela}
        WHERE substr(data, 4, 2) = ?
          AND substr(data, 7, 4) = ?
    """

    if extra_where:
        consulta += " AND " + extra_where

    consulta += """
        GROUP BY categoria
        ORDER BY total DESC, categoria
    """

    registros = conexao.execute(
        consulta,
        (mes, ano)
    ).fetchall()

    dados = [
        {
            "categoria": item["categoria"],
            "total": float(item["total"] or 0)
        }
        for item in registros
    ]

    maior = max(
        (item["total"] for item in dados),
        default=0
    )

    for item in dados:
        item["percentual_barra"] = (
            (item["total"] / maior) * 100
            if maior > 0
            else 0
        )

    return dados


def percentual_parte(valor, total):
    if not total:
        return 0

    return (float(valor) / float(total)) * 100


@app.context_processor
def periodo_global():
    hoje = datetime.now()

    mes_atual = session.get(
        "mes_selecionado",
        hoje.strftime("%m")
    )

    ano_atual = session.get(
        "ano_selecionado",
        str(hoje.year)
    )

    if mes_atual not in MESES:
        mes_atual = hoje.strftime("%m")

    if (
        not str(ano_atual).isdigit()
        or len(str(ano_atual)) != 4
    ):
        ano_atual = str(hoje.year)

    return {
        "periodo_mes": mes_atual,
        "periodo_ano": str(ano_atual),
        "periodo_nome": MESES.get(mes_atual, mes_atual),
        "ultimo_backup": obter_ultimo_backup(),
    }

@app.route("/")
def dashboard():
    if not DB_PATH.exists():
        return render_template("sem_banco.html", caminho=str(DB_PATH))

    garantir_estrutura()
    mes, ano = obter_periodo()

    with conectar() as conexao:
        gerar_despesas_fixas_do_periodo(conexao, mes, ano)

        receitas = soma(conexao, "receitas", mes, ano)
        despesas_pagas = soma(
            conexao,
            "despesas",
            mes,
            ano,
            "COALESCE(pago, 1) = 1"
        )
        despesas_pendentes = soma(
            conexao,
            "despesas",
            mes,
            ano,
            "COALESCE(pago, 1) = 0"
        )

        saldo_anterior = calcular_saldo_mes_anterior(conexao, mes, ano)

        rendimentos_disponiveis = (
            soma_rendimentos_disponiveis_periodo(
                conexao,
                mes,
                ano
            )
        )

        saldo_atual = (
            saldo_anterior
            + receitas
            + rendimentos_disponiveis
            - despesas_pagas
        )

        saldo_previsto = (
            saldo_atual
            - despesas_pendentes
        )

        config_rendimento = (
            obter_config_rendimento(
                conexao
            )
        )

        ultimo_rendimento = (
            obter_ultimo_rendimento_creditado(
                conexao
            )
        )

        estimativa_rendimento = (
            estimar_proximo_rendimento(
                saldo_atual,
                config_rendimento
            )
        )

        despesas_gerais = carregar_despesas(
            conexao,
            mes,
            ano,
            origem="gerais"
        )

        despesas_fixas_lista = carregar_despesas(
            conexao,
            mes,
            ano,
            origem="fixas"
        )

        receitas_lista = conexao.execute("""
            SELECT id, descricao, categoria, valor, data
            FROM receitas
            WHERE substr(data, 4, 2) = ?
              AND substr(data, 7, 4) = ?
            ORDER BY id DESC
        """, (mes, ano)).fetchall()

    return render_template(
        "dashboard.html",
        meses=MESES,
        mes=mes,
        ano=ano,
        anos=obter_anos(ano),
        receitas=receitas,
        despesas_pagas=despesas_pagas,
        despesas_pendentes=despesas_pendentes,
        saldo_anterior=saldo_anterior,
        saldo_atual=saldo_atual,
        saldo_previsto=saldo_previsto,
        config_rendimento=config_rendimento,
        ultimo_rendimento=ultimo_rendimento,
        estimativa_rendimento=estimativa_rendimento,
        despesas_gerais=despesas_gerais,
        despesas_fixas_lista=despesas_fixas_lista,
        receitas_lista=receitas_lista,
    )



@app.route("/rendimento")
def rendimento():
    if not DB_PATH.exists():
        return render_template(
            "sem_banco.html",
            caminho=str(DB_PATH)
        )

    garantir_estrutura()

    with conectar() as conexao:
        config = obter_config_rendimento(
            conexao
        )

        historico = conexao.execute("""
            SELECT
                id,
                data_referencia,
                data_credito,
                saldo_base,
                taxa_anual,
                percentual_cdi,
                taxa_diaria,
                rendimento,
                criado_em,
                COALESCE(rendimento_bruto, rendimento) AS rendimento_bruto,
                COALESCE(aliquota_ir, 0) AS aliquota_ir,
                COALESCE(imposto_ir, 0) AS imposto_ir,
                COALESCE(saldo_elegivel, saldo_base) AS saldo_elegivel
            FROM rendimentos_diarios
            ORDER BY
                substr(data_referencia, 7, 4) DESC,
                substr(data_referencia, 4, 2) DESC,
                substr(data_referencia, 1, 2) DESC,
                id DESC
            LIMIT 30
        """).fetchall()

        total_historico = conexao.execute("""
            SELECT COUNT(*)
            FROM rendimentos_diarios
        """).fetchone()[0]

    data_inicio_html = ""

    if config:
        inicio = parse_data_banco(
            config["data_inicio"]
        )

        if inicio:
            data_inicio_html = (
                inicio.strftime(
                    "%Y-%m-%d"
                )
            )

    taxa_diaria = taxa_diaria_rendimento(
        config["taxa_anual"]
        if config
        else 0,
        config["percentual_cdi"]
        if config
        else 0
    )

    return render_template(
        "rendimento.html",
        config=config,
        historico=historico,
        total_historico=total_historico,
        data_inicio_html=data_inicio_html,
        taxa_diaria_percentual=(
            taxa_diaria
            * 100
        ),
    )



@app.post("/rendimento/recalcular")
def recalcular_rendimento():
    if not DB_PATH.exists():
        return redirect(
            url_for(
                "dashboard"
            )
        )

    garantir_estrutura()

    with conectar() as conexao:
        config = obter_config_rendimento(
            conexao
        )

        if not config:
            flash(
                "Configuração de rendimento não encontrada.",
                "erro"
            )
            return redirect(
                url_for(
                    "rendimento"
                )
            )

        conexao.execute("""
            DELETE FROM rendimentos_diarios
        """)

        conexao.execute("""
            UPDATE rendimento_config
            SET
                processar_a_partir_de = ?,
                aplicar_ir = 1
            WHERE id = 1
        """, (
            config["data_inicio"],
        ))

        conexao.commit()

    processar_rendimentos_pendentes()

    flash(
        "Histórico recalculado com IR regressivo e "
        "saldo elegível no próximo dia útil.",
        "sucesso"
    )

    return redirect(
        url_for(
            "rendimento"
        )
    )

@app.post("/rendimento/salvar")
def salvar_rendimento():
    if not DB_PATH.exists():
        return redirect(
            url_for(
                "dashboard"
            )
        )

    garantir_estrutura()

    ativo = (
        1
        if request.form.get(
            "ativo"
        ) == "1"
        else 0
    )

    try:
        percentual_cdi = float(
            request.form.get(
                "percentual_cdi",
                "100"
            ).replace(
                ",",
                "."
            )
        )

        taxa_anual = float(
            request.form.get(
                "taxa_anual",
                "14"
            ).replace(
                ",",
                "."
            )
        )
    except ValueError:
        flash(
            "Informe taxas válidas.",
            "erro"
        )
        return redirect(
            url_for(
                "rendimento"
            )
        )

    if (
        percentual_cdi < 0
        or percentual_cdi > 500
    ):
        flash(
            "O percentual do CDI deve ficar entre 0% e 500%.",
            "erro"
        )
        return redirect(
            url_for(
                "rendimento"
            )
        )

    if (
        taxa_anual < 0
        or taxa_anual > 100
    ):
        flash(
            "A taxa anual deve ficar entre 0% e 100% ao ano.",
            "erro"
        )
        return redirect(
            url_for(
                "rendimento"
            )
        )

    data_inicio_html = (
        request.form.get(
            "data_inicio",
            ""
        ).strip()
    )

    try:
        data_inicio = datetime.strptime(
            data_inicio_html,
            "%Y-%m-%d"
        ).date()
    except ValueError:
        flash(
            "Informe uma data inicial válida.",
            "erro"
        )
        return redirect(
            url_for(
                "rendimento"
            )
        )

    hoje = datetime.now().date()

    if data_inicio > hoje:
        flash(
            "A data inicial não pode estar no futuro.",
            "erro"
        )
        return redirect(
            url_for(
                "rendimento"
            )
        )

    with conectar() as conexao:
        atual = obter_config_rendimento(
            conexao
        )

        total_historico = conexao.execute("""
            SELECT COUNT(*)
            FROM rendimentos_diarios
        """).fetchone()[0]

        if (
            total_historico > 0
            and atual
        ):
            # Depois que existe histórico, a data inicial fica
            # preservada para não alterar rendimentos já consolidados.
            data_inicio_banco = (
                atual["data_inicio"]
            )
        else:
            data_inicio_banco = (
                formatar_data_banco(
                    data_inicio
                )
            )

        estava_ativo = (
            int(
                atual["ativo"]
                or 0
            )
            if atual
            else 0
        )

        processar_a_partir = (
            atual["processar_a_partir_de"]
            if atual
            else data_inicio_banco
        )

        if (
            ativo == 1
            and estava_ativo == 0
        ):
            if total_historico > 0:
                # Ao reativar, não preenche retroativamente
                # o intervalo em que estava desligado.
                processar_a_partir = (
                    formatar_data_banco(
                        hoje
                    )
                )
            else:
                processar_a_partir = (
                    data_inicio_banco
                )

        conexao.execute("""
            UPDATE rendimento_config
            SET
                ativo = ?,
                percentual_cdi = ?,
                taxa_anual = ?,
                data_inicio = ?,
                processar_a_partir_de = ?,
                atualizado_em = ?,
                aplicar_ir = 1
            WHERE id = 1
        """, (
            ativo,
            percentual_cdi,
            taxa_anual,
            data_inicio_banco,
            processar_a_partir,
            datetime.now().strftime(
                "%d/%m/%Y %H:%M:%S"
            )
        ))

        conexao.commit()

    flash(
        (
            "Rendimento diário ativado."
            if ativo
            else "Rendimento diário desativado."
        ),
        "sucesso"
    )

    return redirect(
        url_for(
            "rendimento"
        )
    )


@app.route("/cartao")
def cartao():
    if not DB_PATH.exists():
        return render_template(
            "sem_banco.html",
            caminho=str(DB_PATH)
        )

    garantir_estrutura()

    editar_id = request.args.get("editar", type=int)
    editar_servico_id = request.args.get("editar_servico", type=int)
    fatura_id = request.args.get("fatura", type=int)
    cartao_id = request.args.get("cartao_id", type=int)
    novo_cartao = request.args.get("novo") == "1"
    mes_fatura, ano_fatura = obter_periodo()

    sincronizar_faturas_cartao()

    with conectar() as conexao:
        cartoes = listar_cartoes(conexao)

        # Fatura ou compra informada sempre define o cartão correspondente.
        if fatura_id:
            fatura_ref = conexao.execute("""
                SELECT cartao_id
                FROM cartao_faturas
                WHERE id = ?
            """, (fatura_id,)).fetchone()
            if fatura_ref:
                cartao_id = int(fatura_ref["cartao_id"])
                competencia_ref = conexao.execute("""
                    SELECT competencia
                    FROM cartao_faturas
                    WHERE id = ?
                """, (fatura_id,)).fetchone()
                if competencia_ref and competencia_ref["competencia"]:
                    mes_fatura = competencia_ref["competencia"][5:7]
                    ano_fatura = competencia_ref["competencia"][0:4]

        if editar_id:
            compra_ref = conexao.execute("""
                SELECT f.cartao_id
                FROM cartao_compras c
                JOIN cartao_faturas f
                    ON f.id = c.fatura_id
                WHERE c.id = ?
            """, (editar_id,)).fetchone()
            if compra_ref:
                cartao_id = int(compra_ref["cartao_id"])

        if editar_servico_id:
            servico_ref = conexao.execute("""
                SELECT cartao_id
                FROM cartao_servicos
                WHERE id = ?
                  AND ativo = 1
            """, (editar_servico_id,)).fetchone()
            if servico_ref:
                cartao_id = int(servico_ref["cartao_id"])

        if novo_cartao:
            config = None
            faturas = []
        else:
            if cartao_id:
                config = obter_config_cartao(conexao, cartao_id)
            else:
                config = obter_config_cartao(conexao)
                cartao_id = int(config["id"]) if config else None

            faturas_todas = (
                resumo_cartao(conexao, cartao_id)
                if config and cartao_id
                else []
            )
            competencia_exibida = f"{int(ano_fatura):04d}-{int(mes_fatura):02d}"
            faturas = [
                item for item in faturas_todas
                if item["competencia"] == competencia_exibida
            ]

        try:
            mes_fatura_int = int(mes_fatura)
            ano_fatura_int = int(ano_fatura)
        except (TypeError, ValueError):
            mes_fatura_int = datetime.now().month
            ano_fatura_int = datetime.now().year
            mes_fatura = f"{mes_fatura_int:02d}"
            ano_fatura = str(ano_fatura_int)

        mes_ant_ano, mes_ant = avancar_competencia_cartao(ano_fatura_int, mes_fatura_int, -1)
        mes_prox_ano, mes_prox = avancar_competencia_cartao(ano_fatura_int, mes_fatura_int, 1)
        periodo_fatura_nome = f"{MESES.get(mes_fatura, mes_fatura)}/{ano_fatura}"

        fatura_selecionada = None

        if fatura_id:
            fatura_selecionada = next(
                (
                    item
                    for item in faturas
                    if int(item["id"]) == int(fatura_id)
                ),
                None
            )

        if fatura_selecionada is None and faturas:
            pendentes = [
                item
                for item in faturas
                if item["status"] != "Paga"
            ]
            fatura_selecionada = pendentes[0] if pendentes else faturas[-1]

        compras = []
        servicos_fatura = []
        if fatura_selecionada:
            # Garantia adicional: se a data da cobrança recorrente já chegou,
            # ela precisa aparecer imediatamente no detalhamento desta fatura.
            garantir_servicos_da_fatura_atual(
                conexao,
                fatura_selecionada["id"]
            )
            sincronizar_fatura_cartao(
                conexao,
                fatura_selecionada["id"]
            )
            conexao.commit()

            # Atualiza somente os campos calculados, preservando o status e
            # demais informações já preparadas por resumo_cartao().
            if fatura_selecionada:
                total_atualizado = conexao.execute("""
                    SELECT
                        COALESCE((SELECT SUM(valor) FROM cartao_parcelas WHERE fatura_id = ?), 0)
                        + COALESCE((SELECT SUM(valor) FROM cartao_servicos_faturas WHERE fatura_id = ?), 0)
                """, (
                    fatura_selecionada["id"],
                    fatura_selecionada["id"]
                )).fetchone()[0] or 0
                # sqlite3.Row é somente leitura; convertemos a fatura para
                # dicionário antes de atualizar o total recalculado.
                fatura_selecionada = dict(fatura_selecionada)
                fatura_selecionada["total"] = float(total_atualizado)

            compras = conexao.execute("""
                SELECT
                    c.id,
                    c.descricao,
                    c.valor AS valor_total,
                    c.parcelas AS total_parcelas,
                    c.categoria,
                    c.data_compra,
                    p.numero_parcela,
                    p.valor AS valor_parcela,
                    p.fatura_id
                FROM cartao_parcelas p
                JOIN cartao_compras c
                    ON c.id = p.compra_id
                WHERE p.fatura_id = ?
                ORDER BY
                    substr(c.data_compra, 7, 4) DESC,
                    substr(c.data_compra, 4, 2) DESC,
                    substr(c.data_compra, 1, 2) DESC,
                    p.numero_parcela ASC,
                    c.id DESC
            """, (fatura_selecionada["id"],)).fetchall()
            servicos_fatura = servicos_da_fatura(
                conexao,
                fatura_selecionada["id"]
            )

        servicos = listar_servicos_cartao(
            conexao,
            cartao_id
        ) if config and cartao_id else []

        compra_edicao = None
        if editar_id:
            registro = conexao.execute("""
                SELECT
                    c.id,
                    c.descricao,
                    c.valor,
                    c.parcelas,
                    c.categoria,
                    c.data_compra,
                    c.fatura_id,
                    f.cartao_id
                FROM cartao_compras c
                JOIN cartao_faturas f
                    ON f.id = c.fatura_id
                WHERE c.id = ?
            """, (editar_id,)).fetchone()

            if registro:
                compra_edicao = dict(registro)
                compra_edicao["data_html"] = converter_data_para_html(
                    compra_edicao["data_compra"]
                )

                fatura_da_edicao = next(
                    (
                        item
                        for item in faturas
                        if int(item["id"]) == int(registro["fatura_id"])
                    ),
                    None
                )
                if fatura_da_edicao:
                    fatura_selecionada = fatura_da_edicao

        servico_edicao = None
        if editar_servico_id:
            registro_servico = conexao.execute("""
                SELECT
                    id,
                    cartao_id,
                    descricao,
                    valor,
                    categoria,
                    data_inicio,
                    ativo
                FROM cartao_servicos
                WHERE id = ?
                  AND ativo = 1
            """, (editar_servico_id,)).fetchone()

            if registro_servico:
                servico_edicao = dict(registro_servico)
                servico_edicao["data_html"] = converter_data_para_html(
                    servico_edicao["data_inicio"]
                )

        faturas_resumo = faturas_todas if config and cartao_id and not novo_cartao else []

        # Os indicadores do topo são calculados somente para o mês/ano
        # atualmente selecionado na navegação da fatura.
        #
        # Fatura em aberto: considera apenas a fatura daquele mês e somente
        # enquanto ela não estiver paga. O total da fatura já contém as
        # parcelas do mês e somente a cobrança mensal dos serviços daquele mês.
        fatura_mes = fatura_selecionada
        fatura_mes_total = (
            float(fatura_mes["total"] or 0)
            if fatura_mes and fatura_mes["status"] != "Paga"
            else 0.0
        )

        # Serviços mensais: apenas o que efetivamente foi lançado na fatura
        # do mês selecionado, nunca a soma dos próximos meses projetados.
        servicos_total = sum(
            float(item["valor"] or 0)
            for item in servicos_fatura
        )

        # Para o limite, uma compra parcelada compromete o limite pelo valor
        # total da compra quando ela aparece na fatura selecionada. Assim,
        # uma compra de R$ 1.200 em 10x compromete R$ 1.200, e não apenas
        # os R$ 120 da parcela. Serviços recorrentes comprometem somente o
        # valor cobrado naquele mês. Se a fatura já foi paga, o compromisso
        # daquele mês deixa de consumir o limite disponível.
        limite_comprometido_mes = 0.0
        if fatura_mes and fatura_mes["status"] != "Paga":
            limite_comprometido_mes = float(conexao.execute("""
                SELECT COALESCE(SUM(c.valor), 0)
                FROM cartao_parcelas p
                JOIN cartao_compras c
                    ON c.id = p.compra_id
                WHERE p.fatura_id = ?
            """, (fatura_mes["id"],)).fetchone()[0] or 0)
            limite_comprometido_mes += servicos_total

        limite = (
            float(config["limite"] or 0)
            if config
            else 0
        )
        limite_disponivel = (
            max(limite - limite_comprometido_mes, 0)
            if limite > 0
            else None
        )

    return render_template(
        "cartao.html",
        cartoes=cartoes,
        config=config,
        faturas=faturas,
        fatura_selecionada=fatura_selecionada,
        compras=compras,
        servicos=servicos,
        servicos_fatura=servicos_fatura,
        servicos_total=servicos_total,
        compra_edicao=compra_edicao,
        servico_edicao=servico_edicao,
        fatura_mes_total=fatura_mes_total,
        limite_comprometido_mes=limite_comprometido_mes,
        limite_disponivel=limite_disponivel,
        cartao_id=cartao_id,
        novo_cartao=novo_cartao,
        data_hoje=datetime.now().strftime("%Y-%m-%d"),
        categorias=obter_nomes_categorias("despesa"),
        meses=MESES,
        mes_fatura=mes_fatura,
        ano_fatura=ano_fatura,
        mes_fatura_anterior=f"{mes_ant:02d}",
        ano_fatura_anterior=str(mes_ant_ano),
        mes_fatura_proxima=f"{mes_prox:02d}",
        ano_fatura_proxima=str(mes_prox_ano),
        periodo_fatura_nome=periodo_fatura_nome,
    )


@app.post("/cartao/configurar")
def salvar_config_cartao():
    if not DB_PATH.exists():
        return redirect(url_for("dashboard"))

    garantir_estrutura()

    cartao_id = request.form.get("cartao_id", type=int)
    nome = request.form.get("nome", "Meu cartão").strip()
    fechamento_texto = request.form.get("dia_fechamento", "").strip()
    vencimento_texto = request.form.get("dia_vencimento", "").strip()
    limite_texto = request.form.get("limite", "").strip()
    ativo = 1 if request.form.get("ativo") == "1" else 0

    if not nome:
        flash("Informe o nome do cartão.", "erro")
        return redirect(url_for("cartao", cartao_id=cartao_id) if cartao_id else url_for("cartao", novo=1))

    try:
        dia_fechamento = int(fechamento_texto)
        dia_vencimento = int(vencimento_texto)
    except ValueError:
        flash("Os dias de fechamento e vencimento devem ser números.", "erro")
        return redirect(url_for("cartao", cartao_id=cartao_id) if cartao_id else url_for("cartao", novo=1))

    if not 1 <= dia_fechamento <= 31:
        flash("O dia de fechamento deve estar entre 1 e 31.", "erro")
        return redirect(url_for("cartao", cartao_id=cartao_id) if cartao_id else url_for("cartao", novo=1))

    if not 1 <= dia_vencimento <= 31:
        flash("O dia de vencimento deve estar entre 1 e 31.", "erro")
        return redirect(url_for("cartao", cartao_id=cartao_id) if cartao_id else url_for("cartao", novo=1))

    try:
        limite = (
            float(limite_texto.replace(".", "").replace(",", "."))
            if limite_texto
            else 0
        )
    except ValueError:
        flash("Digite um limite válido.", "erro")
        return redirect(url_for("cartao", cartao_id=cartao_id) if cartao_id else url_for("cartao", novo=1))

    if limite < 0:
        flash("O limite não pode ser negativo.", "erro")
        return redirect(url_for("cartao", cartao_id=cartao_id) if cartao_id else url_for("cartao", novo=1))

    with conectar() as conexao:
        if cartao_id:
            existente = conexao.execute("""
                SELECT id
                FROM cartoes
                WHERE id = ?
            """, (cartao_id,)).fetchone()
            if not existente:
                flash("Cartão não encontrado.", "erro")
                return redirect(url_for("cartao"))

            conexao.execute("""
                UPDATE cartoes
                SET nome = ?,
                    dia_fechamento = ?,
                    dia_vencimento = ?,
                    limite = ?,
                    ativo = ?
                WHERE id = ?
            """, (
                nome,
                dia_fechamento,
                dia_vencimento,
                limite,
                ativo,
                cartao_id
            ))
            novo_id = cartao_id
            mensagem = "Configuração do cartão atualizada com sucesso."
        else:
            cursor = conexao.execute("""
                INSERT INTO cartoes (
                    nome,
                    dia_fechamento,
                    dia_vencimento,
                    limite,
                    ativo,
                    criado_em
                )
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                nome,
                dia_fechamento,
                dia_vencimento,
                limite,
                ativo,
                datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            ))
            novo_id = cursor.lastrowid
            mensagem = "Novo cartão adicionado com sucesso."

        conexao.commit()

    flash(mensagem, "sucesso")
    return redirect(url_for("cartao", cartao_id=novo_id))


@app.post("/cartao/servicos/salvar")
def salvar_servico_cartao():
    if not DB_PATH.exists():
        return redirect(url_for("dashboard"))

    garantir_estrutura()

    servico_id = request.form.get("id", type=int)
    cartao_id = request.form.get("cartao_id", type=int)
    descricao = request.form.get("descricao", "").strip()
    valor_texto = request.form.get("valor", "").strip()
    categoria = request.form.get("categoria", "").strip()
    data_inicio_html = request.form.get("data_inicio", "").strip()

    if not cartao_id:
        flash("Selecione um cartão antes de cadastrar o serviço.", "erro")
        return redirect(url_for("cartao"))

    if not descricao or not valor_texto or not categoria or not data_inicio_html:
        flash("Preencha todos os campos do serviço mensal.", "erro")
        return redirect(url_for("cartao", cartao_id=cartao_id, editar_servico=servico_id) if servico_id else url_for("cartao", cartao_id=cartao_id))

    try:
        valor = converter_valor_cartao(valor_texto)
    except ValueError:
        flash("Digite um valor válido para o serviço.", "erro")
        return redirect(url_for("cartao", cartao_id=cartao_id, editar_servico=servico_id) if servico_id else url_for("cartao", cartao_id=cartao_id))

    if valor <= 0:
        flash("O valor mensal deve ser maior que zero.", "erro")
        return redirect(url_for("cartao", cartao_id=cartao_id, editar_servico=servico_id) if servico_id else url_for("cartao", cartao_id=cartao_id))

    data_inicio = converter_data_para_banco(data_inicio_html)
    data_inicio_obj = parse_data_banco(data_inicio)
    if not data_inicio_obj:
        flash("Digite uma data de início válida.", "erro")
        return redirect(url_for("cartao", cartao_id=cartao_id, editar_servico=servico_id) if servico_id else url_for("cartao", cartao_id=cartao_id))

    with conectar() as conexao:
        config = obter_config_cartao(conexao, cartao_id)
        if not config:
            flash("Cartão não encontrado.", "erro")
            return redirect(url_for("cartao"))

        if not int(config["ativo"] or 0):
            flash("Ative o cartão antes de cadastrar ou editar serviços mensais.", "erro")
            return redirect(url_for("cartao", cartao_id=cartao_id))

        if servico_id:
            servico_existente = conexao.execute("""
                SELECT
                    id,
                    cartao_id,
                    descricao
                FROM cartao_servicos
                WHERE id = ?
                  AND ativo = 1
            """, (servico_id,)).fetchone()

            if not servico_existente:
                flash("Cobrança recorrente não encontrada.", "erro")
                return redirect(url_for("cartao", cartao_id=cartao_id))

            if int(servico_existente["cartao_id"]) != int(cartao_id):
                flash("A cobrança recorrente pertence a outro cartão.", "erro")
                return redirect(url_for("cartao", cartao_id=servico_existente["cartao_id"]))

            # Histórico pago ou já vencido não é alterado. Apenas as
            # cobranças ainda abertas/futuras são recriadas com os novos dados.
            hoje = datetime.now().date()
            alocacoes_futuras = conexao.execute("""
                SELECT
                    sf.id,
                    sf.fatura_id,
                    f.data_vencimento
                FROM cartao_servicos_faturas sf
                JOIN cartao_faturas f
                    ON f.id = sf.fatura_id
                WHERE sf.servico_id = ?
                  AND f.pago = 0
            """, (servico_id,)).fetchall()

            faturas_para_sincronizar = set()
            for item in alocacoes_futuras:
                vencimento = parse_data_banco(item["data_vencimento"])
                if vencimento and vencimento < hoje:
                    continue
                faturas_para_sincronizar.add(int(item["fatura_id"]))
                conexao.execute("""
                    DELETE FROM cartao_servicos_faturas
                    WHERE id = ?
                """, (item["id"],))

            conexao.execute("""
                UPDATE cartao_servicos
                SET descricao = ?,
                    valor = ?,
                    categoria = ?,
                    data_inicio = ?
                WHERE id = ?
            """, (
                descricao,
                round(valor, 2),
                categoria,
                data_inicio,
                servico_id
            ))

            sincronizar_servicos_cartao(conexao)

            faturas = conexao.execute("""
                SELECT id
                FROM cartao_faturas
                WHERE cartao_id = ?
                ORDER BY data_vencimento ASC, id ASC
            """, (cartao_id,)).fetchall()
            for fatura in faturas:
                sincronizar_fatura_cartao(conexao, fatura["id"])

            conexao.commit()
            mensagem = (
                f"Cobrança recorrente '{descricao}' atualizada. "
                "As próximas faturas foram recalculadas sem alterar o histórico já pago ou vencido."
            )
        else:
            conexao.execute("""
                INSERT INTO cartao_servicos (
                    cartao_id,
                    descricao,
                    valor,
                    categoria,
                    data_inicio,
                    ativo,
                    criado_em
                )
                VALUES (?, ?, ?, ?, ?, 1, ?)
            """, (
                cartao_id,
                descricao,
                round(valor, 2),
                categoria,
                data_inicio,
                datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            ))

            sincronizar_servicos_cartao(conexao)
            faturas = conexao.execute("""
                SELECT id
                FROM cartao_faturas
                WHERE cartao_id = ?
                ORDER BY data_vencimento ASC, id ASC
            """, (cartao_id,)).fetchall()
            for fatura in faturas:
                sincronizar_fatura_cartao(conexao, fatura["id"])
            conexao.commit()

            mensagem = (
                f"Serviço mensal '{descricao}' adicionado. "
                f"Ele será incluído automaticamente nas próximas faturas de {config['nome']}."
            )

    flash(mensagem, "sucesso")
    return redirect(url_for("cartao", cartao_id=cartao_id))


@app.post("/cartao/servicos/<int:servico_id>/excluir")
def excluir_servico_cartao(servico_id):
    if not DB_PATH.exists():
        return redirect(url_for("dashboard"))

    garantir_estrutura()

    with conectar() as conexao:
        servico = conexao.execute("""
            SELECT
                id,
                cartao_id,
                descricao
            FROM cartao_servicos
            WHERE id = ?
              AND ativo = 1
        """, (servico_id,)).fetchone()

        if not servico:
            flash("Serviço mensal não encontrado.", "erro")
            return redirect(url_for("cartao"))

        hoje = datetime.now().date()
        alocacoes = conexao.execute("""
            SELECT
                sf.id,
                sf.fatura_id,
                f.pago,
                f.data_vencimento
            FROM cartao_servicos_faturas sf
            JOIN cartao_faturas f
                ON f.id = sf.fatura_id
            WHERE sf.servico_id = ?
        """, (servico_id,)).fetchall()

        faturas_para_sincronizar = set()

        for item in alocacoes:
            vencimento = parse_data_banco(item["data_vencimento"])
            fatura_paga = int(item["pago"] or 0) == 1

            # Cobranças já pagas ou já vencidas são histórico e permanecem.
            if fatura_paga or (vencimento and vencimento < hoje):
                conexao.execute("""
                    UPDATE cartao_servicos_faturas
                    SET servico_id = NULL
                    WHERE id = ?
                """, (item["id"],))
            else:
                conexao.execute("""
                    DELETE FROM cartao_servicos_faturas
                    WHERE id = ?
                """, (item["id"],))
                faturas_para_sincronizar.add(int(item["fatura_id"]))

        # O serviço some da tabela de serviços ativos, mas seu histórico já
        # lançado continua guardado nas faturas.
        conexao.execute("""
            DELETE FROM cartao_servicos
            WHERE id = ?
        """, (servico_id,))

        for fatura_id in sorted(faturas_para_sincronizar):
            sincronizar_fatura_cartao(conexao, fatura_id)

        conexao.commit()

    flash(
        f"Serviço mensal '{servico['descricao']}' encerrado. As próximas faturas não terão mais essa cobrança.",
        "sucesso"
    )
    return redirect(url_for("cartao", cartao_id=servico["cartao_id"]))


@app.post("/cartao/compras/salvar")
def salvar_compra_cartao():
    if not DB_PATH.exists():
        return redirect(url_for("dashboard"))

    garantir_estrutura()

    compra_id = request.form.get("id", type=int)
    cartao_id = request.form.get("cartao_id", type=int)
    descricao = request.form.get("descricao", "").strip()
    valor_texto = request.form.get("valor", "").strip()
    parcelas_texto = request.form.get("parcelas", "1").strip()
    categoria = request.form.get("categoria", "").strip()
    data_html = request.form.get("data_compra", "").strip()
    fatura_retorno = request.form.get("fatura", type=int)

    retorno = lambda fid=fatura_retorno, cid=cartao_id: url_for(
        "cartao",
        fatura=fid,
        cartao_id=cid
    )

    if not descricao or not valor_texto or not categoria or not data_html:
        flash("Preencha todos os campos da compra.", "erro")
        return redirect(retorno())

    try:
        valor = converter_valor_cartao(valor_texto)
    except ValueError:
        flash("Digite um valor válido.", "erro")
        return redirect(retorno())

    try:
        parcelas = int(parcelas_texto)
    except ValueError:
        parcelas = 0

    if valor <= 0:
        flash("O valor total da compra deve ser maior que zero.", "erro")
        return redirect(retorno())

    if not 1 <= parcelas <= 60:
        flash("O número de parcelas deve estar entre 1 e 60.", "erro")
        return redirect(retorno())

    valor = round(valor, 2)
    data_banco = converter_data_para_banco(data_html)

    if not data_banco:
        flash("Digite uma data de compra válida.", "erro")
        return redirect(retorno())

    data_obj = parse_data_banco(data_banco)
    if not data_obj:
        flash("Digite uma data de compra válida.", "erro")
        return redirect(retorno())

    with conectar() as conexao:
        # Compatibilidade: se o formulário não trouxer o cartão, descobrimos
        # pelo contexto da fatura ou da compra que está sendo editada.
        if not cartao_id and fatura_retorno:
            ref = conexao.execute("""
                SELECT cartao_id
                FROM cartao_faturas
                WHERE id = ?
            """, (fatura_retorno,)).fetchone()
            if ref:
                cartao_id = int(ref["cartao_id"])

        registro_antigo = None
        faturas_antigas = []

        if compra_id:
            registro_antigo = conexao.execute("""
                SELECT
                    c.id,
                    c.fatura_id,
                    f.cartao_id,
                    EXISTS (
                        SELECT 1
                        FROM cartao_parcelas p
                        JOIN cartao_faturas fp
                            ON fp.id = p.fatura_id
                        WHERE p.compra_id = c.id
                          AND fp.pago = 1
                    ) AS tem_fatura_paga
                FROM cartao_compras c
                JOIN cartao_faturas f
                    ON f.id = c.fatura_id
                WHERE c.id = ?
            """, (compra_id,)).fetchone()

            if not registro_antigo:
                flash("Compra não encontrada.", "erro")
                return redirect(retorno())

            cartao_original_id = int(registro_antigo["cartao_id"])
            if cartao_id and cartao_id != cartao_original_id:
                flash("Não é possível trocar o cartão de uma compra já lançada. Edite a compra dentro do cartão correto.", "erro")
                return redirect(url_for("cartao", cartao_id=cartao_original_id))

            cartao_id = cartao_original_id

            if int(registro_antigo["tem_fatura_paga"] or 0):
                flash("Não é possível alterar uma compra que já possui parcela em fatura paga.", "erro")
                return redirect(url_for("cartao", cartao_id=cartao_id, fatura=registro_antigo["fatura_id"]))

            faturas_antigas = [
                int(item["fatura_id"])
                for item in conexao.execute("""
                    SELECT DISTINCT fatura_id
                    FROM cartao_parcelas
                    WHERE compra_id = ?
                """, (compra_id,)).fetchall()
            ]

        config = obter_config_cartao(conexao, cartao_id)

        if not config or not int(config["ativo"] or 0):
            flash("Configure e ative o cartão antes de lançar compras.", "erro")
            return redirect(url_for("cartao", cartao_id=cartao_id))

        competencias = calcular_competencias_parcelas_cartao(
            data_obj,
            config["dia_fechamento"],
            parcelas
        )

        faturas_destino = []
        for ano, mes in competencias:
            competencia = f"{int(ano):04d}-{int(mes):02d}"
            existente = conexao.execute("""
                SELECT *
                FROM cartao_faturas
                WHERE cartao_id = ?
                  AND competencia = ?
            """, (cartao_id, competencia)).fetchone()

            if existente and int(existente["pago"] or 0):
                flash(
                    f"A parcela cairia na fatura {competencia[5:]}/{competencia[:4]}, que já está paga. Escolha outra data ou revise o fechamento.",
                    "erro"
                )
                return redirect(url_for("cartao", cartao_id=cartao_id, fatura=existente["id"]))

            faturas_destino.append((ano, mes, existente))

        faturas_resolvidas = []
        for ano, mes, existente in faturas_destino:
            fatura = existente or obter_ou_criar_fatura_cartao(
                conexao,
                ano,
                mes,
                config
            )
            faturas_resolvidas.append((ano, mes, fatura))

        valores_parcelas = valores_parcelas_cartao(valor, parcelas)

        if compra_id:
            conexao.execute("""
                UPDATE cartao_compras
                SET descricao = ?,
                    valor = ?,
                    parcelas = ?,
                    categoria = ?,
                    data_compra = ?,
                    fatura_id = ?
                WHERE id = ?
            """, (
                descricao,
                valor,
                parcelas,
                categoria,
                data_banco,
                faturas_resolvidas[0][2]["id"],
                compra_id
            ))
            compra_nova_id = compra_id
        else:
            cursor = conexao.execute("""
                INSERT INTO cartao_compras (
                    descricao,
                    valor,
                    categoria,
                    data_compra,
                    fatura_id,
                    criado_em,
                    parcelas
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                descricao,
                valor,
                categoria,
                data_banco,
                faturas_resolvidas[0][2]["id"],
                datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                parcelas
            ))
            compra_nova_id = cursor.lastrowid

        conexao.execute("""
            DELETE FROM cartao_parcelas
            WHERE compra_id = ?
        """, (compra_nova_id,))

        for indice, valor_parcela in enumerate(valores_parcelas, start=1):
            fatura_destino_id = int(faturas_resolvidas[indice - 1][2]["id"])
            conexao.execute("""
                INSERT INTO cartao_parcelas (
                    compra_id,
                    numero_parcela,
                    valor,
                    fatura_id
                )
                VALUES (?, ?, ?, ?)
            """, (
                compra_nova_id,
                indice,
                valor_parcela,
                fatura_destino_id
            ))

        faturas_para_sincronizar = set(faturas_antigas)
        faturas_para_sincronizar.update(
            int(item[2]["id"])
            for item in faturas_resolvidas
        )

        for fatura_id_item in sorted(faturas_para_sincronizar):
            sincronizar_fatura_cartao(conexao, fatura_id_item)

        conexao.commit()
        fatura_destino = int(faturas_resolvidas[0][2]["id"])

    mensagem = (
        f"Compra adicionada em {parcelas} parcelas. Cada fatura receberá sua parcela automaticamente."
        if parcelas > 1 and not compra_id
        else f"Compra atualizada para {parcelas} parcelas."
        if parcelas > 1 and compra_id
        else "Compra de cartão atualizada."
        if compra_id
        else "Compra adicionada à fatura automaticamente."
    )

    flash(mensagem, "sucesso")
    return redirect(url_for("cartao", cartao_id=cartao_id, fatura=fatura_destino))


@app.post("/cartao/compras/<int:compra_id>/excluir")
def excluir_compra_cartao(compra_id):
    if not DB_PATH.exists():
        return redirect(url_for("dashboard"))

    garantir_estrutura()

    with conectar() as conexao:
        registro = conexao.execute("""
            SELECT
                c.id,
                c.fatura_id,
                f.cartao_id
            FROM cartao_compras c
            JOIN cartao_faturas f
                ON f.id = c.fatura_id
            WHERE c.id = ?
        """, (compra_id,)).fetchone()

        if not registro:
            flash("Compra não encontrada.", "erro")
            return redirect(url_for("cartao"))

        faturas_compra = [
            int(item["fatura_id"])
            for item in conexao.execute("""
                SELECT DISTINCT p.fatura_id
                FROM cartao_parcelas p
                JOIN cartao_faturas f
                    ON f.id = p.fatura_id
                WHERE p.compra_id = ?
            """, (compra_id,)).fetchall()
        ]

        if not faturas_compra:
            faturas_compra = [int(registro["fatura_id"])]

        fatura_paga = conexao.execute("""
            SELECT f.id
            FROM cartao_parcelas p
            JOIN cartao_faturas f
                ON f.id = p.fatura_id
            WHERE p.compra_id = ?
              AND f.pago = 1
            LIMIT 1
        """, (compra_id,)).fetchone()

        if fatura_paga:
            flash("Não é possível excluir uma compra que já possui parcela em fatura paga.", "erro")
            return redirect(url_for("cartao", cartao_id=registro["cartao_id"], fatura=fatura_paga["id"]))

        conexao.execute("DELETE FROM cartao_parcelas WHERE compra_id = ?", (compra_id,))
        conexao.execute("DELETE FROM cartao_compras WHERE id = ?", (compra_id,))

        for fatura_id_item in faturas_compra:
            sincronizar_fatura_cartao(conexao, fatura_id_item)

        conexao.commit()

    flash("Compra e suas parcelas removidas das faturas.", "sucesso")
    return redirect(url_for(
        "cartao",
        cartao_id=registro["cartao_id"],
        fatura=faturas_compra[0] if faturas_compra else None
    ))


@app.post("/cartao/faturas/<int:fatura_id>/status")
def alterar_status_fatura_cartao(fatura_id):
    if not DB_PATH.exists():
        return redirect(url_for("dashboard"))

    garantir_estrutura()

    acao = request.form.get("acao", "")

    if acao not in {"pagar", "pendente"}:
        flash("Ação inválida.", "erro")
        return redirect(url_for("cartao", fatura=fatura_id))

    with conectar() as conexao:
        fatura = conexao.execute("""
            SELECT
                id,
                cartao_id,
                despesa_id
            FROM cartao_faturas
            WHERE id = ?
        """, (fatura_id,)).fetchone()

        if not fatura:
            flash("Fatura não encontrada.", "erro")
            return redirect(url_for("cartao"))

        pago = 1 if acao == "pagar" else 0
        data_pagamento = datetime.now().strftime("%d/%m/%Y") if pago else None

        conexao.execute("""
            UPDATE cartao_faturas
            SET pago = ?,
                data_pagamento = ?
            WHERE id = ?
        """, (pago, data_pagamento, fatura_id))

        sincronizar_fatura_cartao(conexao, fatura_id)
        conexao.commit()

    flash(
        "Fatura marcada como paga." if pago else "Fatura voltou para pendente.",
        "sucesso"
    )

    return redirect(url_for(
        "cartao",
        cartao_id=fatura["cartao_id"],
        fatura=fatura_id
    ))


@app.route("/categorias")
def categorias():
    if not DB_PATH.exists():
        return render_template(
            "sem_banco.html",
            caminho=str(DB_PATH)
        )

    garantir_estrutura()

    # Mantém o mesmo período selecionado durante a navegação,
    # apesar de categorias serem uma configuração global.
    mes, ano = obter_periodo()

    with conectar() as conexao:
        categorias_despesas = listar_categorias_com_uso(
            conexao,
            "despesa"
        )

        categorias_receitas = listar_categorias_com_uso(
            conexao,
            "receita"
        )

    return render_template(
        "categorias.html",
        mes=mes,
        ano=ano,
        categorias_despesas=categorias_despesas,
        categorias_receitas=categorias_receitas,
    )


@app.post("/categorias/adicionar")
def adicionar_categoria():
    garantir_estrutura()

    tipo = request.form.get(
        "tipo",
        ""
    ).strip()

    nome = " ".join(
        request.form.get(
            "nome",
            ""
        ).split()
    )

    if tipo not in {
        "despesa",
        "receita"
    }:
        flash(
            "Tipo de categoria inválido.",
            "erro"
        )

        return redirect(
            url_for(
                "categorias",
                mes=session.get(
                    "mes_selecionado"
                ),
                ano=session.get(
                    "ano_selecionado"
                ),
            )
        )

    if not nome:
        flash(
            "Digite o nome da categoria.",
            "erro"
        )

        return redirect(
            url_for(
                "categorias",
                mes=session.get(
                    "mes_selecionado"
                ),
                ano=session.get(
                    "ano_selecionado"
                ),
            )
        )

    try:
        with conectar() as conexao:
            conexao.execute("""
                INSERT INTO categorias (
                    nome,
                    tipo
                )
                VALUES (?, ?)
            """, (
                nome,
                tipo
            ))

            conexao.commit()

        flash(
            "Categoria adicionada com sucesso.",
            "sucesso"
        )

    except sqlite3.IntegrityError:
        flash(
            "Já existe uma categoria com esse nome.",
            "erro"
        )

    return redirect(
        url_for(
            "categorias",
            mes=session.get(
                "mes_selecionado"
            ),
            ano=session.get(
                "ano_selecionado"
            ),
        )
    )


@app.post("/categorias/<int:categoria_id>/renomear")
def renomear_categoria(categoria_id):
    garantir_estrutura()

    novo_nome = " ".join(
        request.form.get(
            "novo_nome",
            ""
        ).split()
    )

    if not novo_nome:
        flash(
            "Digite o novo nome da categoria.",
            "erro"
        )

        return redirect(
            url_for(
                "categorias",
                mes=session.get(
                    "mes_selecionado"
                ),
                ano=session.get(
                    "ano_selecionado"
                ),
            )
        )

    with conectar() as conexao:
        categoria = conexao.execute("""
            SELECT
                id,
                nome,
                tipo
            FROM categorias
            WHERE id = ?
        """, (
            categoria_id,
        )).fetchone()

        if not categoria:
            flash(
                "Categoria não encontrada.",
                "erro"
            )

            return redirect(
                url_for(
                    "categorias",
                    mes=session.get(
                        "mes_selecionado"
                    ),
                    ano=session.get(
                        "ano_selecionado"
                    ),
                )
            )

        duplicada = conexao.execute("""
            SELECT id
            FROM categorias
            WHERE tipo = ?
              AND nome = ? COLLATE NOCASE
              AND id <> ?
        """, (
            categoria["tipo"],
            novo_nome,
            categoria_id
        )).fetchone()

        if duplicada:
            flash(
                "Já existe outra categoria com esse nome.",
                "erro"
            )

            return redirect(
                url_for(
                    "categorias",
                    mes=session.get(
                        "mes_selecionado"
                    ),
                    ano=session.get(
                        "ano_selecionado"
                    ),
                )
            )

        atualizar_categoria_nos_lancamentos(
            conexao,
            categoria["tipo"],
            categoria["nome"],
            novo_nome
        )

        conexao.execute("""
            UPDATE categorias
            SET nome = ?
            WHERE id = ?
        """, (
            novo_nome,
            categoria_id
        ))

        conexao.commit()

    flash(
        "Categoria renomeada. Os lançamentos antigos também foram atualizados.",
        "sucesso"
    )

    return redirect(
        url_for(
            "categorias",
            mes=session.get(
                "mes_selecionado"
            ),
            ano=session.get(
                "ano_selecionado"
            ),
        )
    )


@app.post("/categorias/<int:categoria_id>/excluir")
def excluir_categoria(categoria_id):
    garantir_estrutura()

    substituta_id = request.form.get(
        "substituta_id",
        type=int
    )

    with conectar() as conexao:
        categoria = conexao.execute("""
            SELECT
                id,
                nome,
                tipo
            FROM categorias
            WHERE id = ?
        """, (
            categoria_id,
        )).fetchone()

        if not categoria:
            flash(
                "Categoria não encontrada.",
                "erro"
            )

            return redirect(
                url_for(
                    "categorias",
                    mes=session.get(
                        "mes_selecionado"
                    ),
                    ano=session.get(
                        "ano_selecionado"
                    ),
                )
            )

        uso = contar_uso_categoria(
            conexao,
            categoria["tipo"],
            categoria["nome"]
        )

        substituta = None

        if substituta_id:
            substituta = conexao.execute("""
                SELECT
                    id,
                    nome,
                    tipo
                FROM categorias
                WHERE id = ?
                  AND tipo = ?
                  AND id <> ?
            """, (
                substituta_id,
                categoria["tipo"],
                categoria_id
            )).fetchone()

        if uso > 0 and not substituta:
            flash(
                "Essa categoria está em uso. "
                "Escolha outra categoria para receber os lançamentos antes de excluir.",
                "erro"
            )

            return redirect(
                url_for(
                    "categorias",
                    mes=session.get(
                        "mes_selecionado"
                    ),
                    ano=session.get(
                        "ano_selecionado"
                    ),
                )
            )

        if uso > 0:
            atualizar_categoria_nos_lancamentos(
                conexao,
                categoria["tipo"],
                categoria["nome"],
                substituta["nome"]
            )

        conexao.execute("""
            DELETE FROM categorias
            WHERE id = ?
        """, (
            categoria_id,
        ))

        conexao.commit()

    if uso > 0:
        flash(
            "Categoria excluída e os lançamentos foram movidos para "
            f'"{substituta["nome"]}".',
            "sucesso"
        )
    else:
        flash(
            "Categoria excluída com sucesso.",
            "sucesso"
        )

    return redirect(
        url_for(
            "categorias",
            mes=session.get(
                "mes_selecionado"
            ),
            ano=session.get(
                "ano_selecionado"
            ),
        )
    )


@app.route("/exportar")
def exportar():
    if not DB_PATH.exists():
        return render_template(
            "sem_banco.html",
            caminho=str(DB_PATH)
        )

    garantir_estrutura()
    mes, ano = obter_periodo()

    with conectar() as conexao:
        gerar_despesas_fixas_do_periodo(
            conexao,
            mes,
            ano
        )

        qtd_despesas = conexao.execute("""
            SELECT COUNT(*)
            FROM despesas
            WHERE substr(data, 4, 2) = ?
              AND substr(data, 7, 4) = ?
        """, (
            mes,
            ano
        )).fetchone()[0]

        qtd_receitas = conexao.execute("""
            SELECT COUNT(*)
            FROM receitas
            WHERE substr(data, 4, 2) = ?
              AND substr(data, 7, 4) = ?
        """, (
            mes,
            ano
        )).fetchone()[0]

        total_historico_despesas = conexao.execute("""
            SELECT COUNT(*)
            FROM despesas
        """).fetchone()[0]

        total_historico_receitas = conexao.execute("""
            SELECT COUNT(*)
            FROM receitas
        """).fetchone()[0]

        total_recorrencias = conexao.execute("""
            SELECT COUNT(*)
            FROM despesas_fixas
        """).fetchone()[0]

    return render_template(
        "exportar.html",
        meses=MESES,
        mes=mes,
        ano=ano,
        anos=obter_anos(ano),
        qtd_despesas=qtd_despesas,
        qtd_receitas=qtd_receitas,
        total_historico_despesas=total_historico_despesas,
        total_historico_receitas=total_historico_receitas,
        total_recorrencias=total_recorrencias,
    )


@app.route("/exportar/mes/consolidado.csv")
def exportar_mes_consolidado():
    if not DB_PATH.exists():
        return redirect(
            url_for("dashboard")
        )

    garantir_estrutura()
    mes, ano = obter_periodo()

    with conectar() as conexao:
        gerar_despesas_fixas_do_periodo(
            conexao,
            mes,
            ano
        )

        despesas = carregar_despesas(
            conexao,
            mes,
            ano
        )

        receitas = conexao.execute("""
            SELECT
                id,
                descricao,
                valor,
                categoria,
                data
            FROM receitas
            WHERE substr(data, 4, 2) = ?
              AND substr(data, 7, 4) = ?
            ORDER BY
                substr(data, 1, 2),
                id
        """, (
            mes,
            ano
        )).fetchall()

    linhas = []

    for item in receitas:
        linhas.append({
            "data": item["data"] or "",
            "ordem": 0,
            "linha": [
                "Entrada",
                "Receita",
                item["descricao"],
                item["categoria"],
                valor_csv(
                    item["valor"]
                ),
                item["data"] or "",
                "Recebida",
                "",
            ]
        })

    for item in despesas:
        linhas.append({
            "data": item["data"] or "",
            "ordem": 1,
            "linha": [
                "Saída",
                item["origem"],
                item["descricao"],
                item["categoria"],
                valor_csv(
                    item["valor"]
                ),
                item["data"] or "",
                item["status"],
                item["data_pagamento"] or "",
            ]
        })

    def chave_data(registro):
        try:
            data = datetime.strptime(
                registro["data"],
                "%d/%m/%Y"
            )
        except (TypeError, ValueError):
            data = datetime.max

        return (
            data,
            registro["ordem"]
        )

    linhas.sort(
        key=chave_data
    )

    nome = (
        f"controle_financeiro_{ano}_{mes}_consolidado.csv"
    )

    return resposta_csv(
        nome,
        [
            "Tipo",
            "Origem",
            "Descrição",
            "Categoria",
            "Valor (R$)",
            "Data",
            "Status",
            "Data de pagamento",
        ],
        [
            registro["linha"]
            for registro in linhas
        ]
    )


@app.route("/exportar/mes/arquivos.zip")
def exportar_mes_arquivos():
    if not DB_PATH.exists():
        return redirect(
            url_for("dashboard")
        )

    garantir_estrutura()
    mes, ano = obter_periodo()

    with conectar() as conexao:
        gerar_despesas_fixas_do_periodo(
            conexao,
            mes,
            ano
        )

        despesas_gerais = carregar_despesas(
            conexao,
            mes,
            ano,
            origem="gerais"
        )

        despesas_fixas = carregar_despesas(
            conexao,
            mes,
            ano,
            origem="fixas"
        )

        receitas = conexao.execute("""
            SELECT
                id,
                descricao,
                valor,
                categoria,
                data
            FROM receitas
            WHERE substr(data, 4, 2) = ?
              AND substr(data, 7, 4) = ?
            ORDER BY
                substr(data, 1, 2),
                id
        """, (
            mes,
            ano
        )).fetchall()

    memoria = BytesIO()

    with zipfile.ZipFile(
        memoria,
        "w",
        compression=zipfile.ZIP_DEFLATED
    ) as arquivo_zip:

        cabecalho_despesas = [
            "ID",
            "Descrição",
            "Categoria",
            "Valor (R$)",
            "Data",
            "Status",
            "Data de pagamento",
            "Origem",
        ]

        adicionar_csv_ao_zip(
            arquivo_zip,
            "despesas_gerais.csv",
            cabecalho_despesas,
            linhas_despesas_exportacao(
                despesas_gerais
            )
        )

        adicionar_csv_ao_zip(
            arquivo_zip,
            "despesas_fixas.csv",
            cabecalho_despesas,
            linhas_despesas_exportacao(
                despesas_fixas
            )
        )

        adicionar_csv_ao_zip(
            arquivo_zip,
            "receitas.csv",
            [
                "ID",
                "Descrição",
                "Categoria",
                "Valor (R$)",
                "Data",
            ],
            linhas_receitas_exportacao(
                receitas
            )
        )

    memoria.seek(0)

    nome = (
        f"controle_financeiro_{ano}_{mes}.zip"
    )

    return send_file(
        memoria,
        mimetype="application/zip",
        as_attachment=True,
        download_name=nome
    )


@app.route("/exportar/historico.zip")
def exportar_historico():
    if not DB_PATH.exists():
        return redirect(
            url_for("dashboard")
        )

    garantir_estrutura()

    with conectar() as conexao:

        registros_despesas = conexao.execute("""
            SELECT
                id,
                descricao,
                categoria,
                valor,
                data,
                COALESCE(pago, 1) AS pago,
                data_pagamento,
                despesa_fixa_id
            FROM despesas
            ORDER BY
                substr(data, 7, 4),
                substr(data, 4, 2),
                substr(data, 1, 2),
                id
        """).fetchall()

        despesas = []

        for item in registros_despesas:
            registro = dict(
                item
            )

            registro["status"] = status_despesa(
                item
            )

            registro["origem"] = (
                "Fixa"
                if item["despesa_fixa_id"] is not None
                else "Manual"
            )

            despesas.append(
                registro
            )

        receitas = conexao.execute("""
            SELECT
                id,
                descricao,
                categoria,
                valor,
                data
            FROM receitas
            ORDER BY
                substr(data, 7, 4),
                substr(data, 4, 2),
                substr(data, 1, 2),
                id
        """).fetchall()

        recorrencias = conexao.execute("""
            SELECT
                id,
                descricao,
                categoria,
                valor,
                dia_vencimento,
                data_inicio,
                ativa
            FROM despesas_fixas
            ORDER BY
                descricao COLLATE NOCASE,
                id
        """).fetchall()

        categorias = conexao.execute("""
            SELECT
                tipo,
                nome
            FROM categorias
            ORDER BY
                tipo,
                nome COLLATE NOCASE
        """).fetchall()


        rendimentos_exportacao = conexao.execute("""
            SELECT
                data_referencia,
                data_credito,
                saldo_base,
                taxa_anual,
                percentual_cdi,
                taxa_diaria,
                rendimento,
                COALESCE(rendimento_bruto, rendimento) AS rendimento_bruto,
                COALESCE(aliquota_ir, 0) AS aliquota_ir,
                COALESCE(imposto_ir, 0) AS imposto_ir,
                COALESCE(saldo_elegivel, saldo_base) AS saldo_elegivel
            FROM rendimentos_diarios
            ORDER BY
                substr(data_referencia, 7, 4),
                substr(data_referencia, 4, 2),
                substr(data_referencia, 1, 2),
                id
        """).fetchall()

    memoria = BytesIO()

    with zipfile.ZipFile(
        memoria,
        "w",
        compression=zipfile.ZIP_DEFLATED
    ) as arquivo_zip:

        adicionar_csv_ao_zip(
            arquivo_zip,
            "despesas.csv",
            [
                "ID",
                "Descrição",
                "Categoria",
                "Valor (R$)",
                "Data",
                "Status",
                "Data de pagamento",
                "Origem",
            ],
            linhas_despesas_exportacao(
                despesas
            )
        )

        adicionar_csv_ao_zip(
            arquivo_zip,
            "receitas.csv",
            [
                "ID",
                "Descrição",
                "Categoria",
                "Valor (R$)",
                "Data",
            ],
            linhas_receitas_exportacao(
                receitas
            )
        )

        adicionar_csv_ao_zip(
            arquivo_zip,
            "recorrencias.csv",
            [
                "ID",
                "Descrição",
                "Categoria",
                "Valor padrão (R$)",
                "Dia do vencimento",
                "Data de início",
                "Situação",
            ],
            [
                [
                    item["id"],
                    item["descricao"],
                    item["categoria"],
                    valor_csv(
                        item["valor"]
                    ),
                    item["dia_vencimento"],
                    item["data_inicio"] or "",
                    (
                        "Ativa"
                        if int(item["ativa"] or 0) == 1
                        else "Inativa"
                    ),
                ]
                for item in recorrencias
            ]
        )

        adicionar_csv_ao_zip(
            arquivo_zip,
            "categorias.csv",
            [
                "Tipo",
                "Nome",
            ],
            [
                [
                    (
                        "Despesa"
                        if item["tipo"] == "despesa"
                        else "Receita"
                    ),
                    item["nome"],
                ]
                for item in categorias
            ]
        )

        adicionar_csv_ao_zip(
            arquivo_zip,
            "rendimentos_cdi.csv",
            [
                "Data de referência",
                "Data de crédito",
                "Saldo base (R$)",
                "Taxa anual (%)",
                "Percentual CDI (%)",
                "Taxa diária (%)",
                "Rendimento bruto (R$)",
                "Alíquota IR (%)",
                "IR retido (R$)",
                "Rendimento líquido (R$)",
            ],
            [
                [
                    item["data_referencia"],
                    item["data_credito"],
                    valor_csv(
                        item["saldo_base"]
                    ),
                    str(
                        float(
                            item["taxa_anual"]
                            or 0
                        )
                    ).replace(".", ","),
                    str(
                        float(
                            item["percentual_cdi"]
                            or 0
                        )
                    ).replace(".", ","),
                    str(
                        float(
                            item["taxa_diaria"]
                            or 0
                        )
                        * 100
                    ).replace(".", ","),
                    valor_csv(
                        item["rendimento_bruto"]
                    ),
                    str(
                        float(
                            item["aliquota_ir"]
                            or 0
                        )
                    ).replace(".", ","),
                    valor_csv(
                        item["imposto_ir"]
                    ),
                    valor_csv(
                        item["rendimento"]
                    ),
                ]
                for item in rendimentos_exportacao
            ]
        )

        adicionar_csv_ao_zip(
            arquivo_zip,
            "LEIA-ME.csv",
            [
                "Informação",
                "Valor",
            ],
            [
                [
                    "Gerado em",
                    datetime.now().strftime(
                        "%d/%m/%Y %H:%M"
                    ),
                ],
                [
                    "Formato",
                    "CSV UTF-8 separado por ponto e vírgula",
                ],
                [
                    "Compatibilidade",
                    "Excel, LibreOffice, Google Planilhas e outros",
                ],
                [
                    "Observação",
                    "Este arquivo é uma exportação; o banco despesas.db não foi alterado.",
                ],
            ]
        )

    memoria.seek(0)

    nome = (
        "controle_financeiro_historico_"
        + datetime.now().strftime(
            "%Y-%m-%d"
        )
        + ".zip"
    )

    return send_file(
        memoria,
        mimetype="application/zip",
        as_attachment=True,
        download_name=nome
    )


@app.route("/relatorios")
def relatorios():
    if not DB_PATH.exists():
        return render_template(
            "sem_banco.html",
            caminho=str(DB_PATH)
        )

    garantir_estrutura()
    mes, ano = obter_periodo()

    with conectar() as conexao:

        # Garante que as recorrências do período selecionado
        # estejam refletidas nos relatórios.
        gerar_despesas_fixas_do_periodo(
            conexao,
            mes,
            ano
        )

        total_receitas = soma(
            conexao,
            "receitas",
            mes,
            ano
        )

        total_gerais = soma(
            conexao,
            "despesas",
            mes,
            ano,
            "despesa_fixa_id IS NULL"
        )

        total_fixas = soma(
            conexao,
            "despesas",
            mes,
            ano,
            "despesa_fixa_id IS NOT NULL"
        )

        total_pagas = soma(
            conexao,
            "despesas",
            mes,
            ano,
            "COALESCE(pago, 1) = 1"
        )

        total_pendentes = soma(
            conexao,
            "despesas",
            mes,
            ano,
            "COALESCE(pago, 1) = 0"
        )

        total_despesas = (
            total_gerais
            + total_fixas
        )

        resultado_periodo = (
            total_receitas
            - total_despesas
        )

        despesas_gerais_categoria = (
            agrupar_por_categoria(
                conexao,
                "despesas",
                mes,
                ano,
                "despesa_fixa_id IS NULL"
            )
        )

        despesas_fixas_categoria = (
            agrupar_por_categoria(
                conexao,
                "despesas",
                mes,
                ano,
                "despesa_fixa_id IS NOT NULL"
            )
        )

        despesas_total_categoria = (
            agrupar_por_categoria(
                conexao,
                "despesas",
                mes,
                ano
            )
        )

        receitas_categoria = (
            agrupar_por_categoria(
                conexao,
                "receitas",
                mes,
                ano
            )
        )

    percentual_gerais = percentual_parte(
        total_gerais,
        total_despesas
    )

    percentual_fixas = percentual_parte(
        total_fixas,
        total_despesas
    )

    percentual_pagas = percentual_parte(
        total_pagas,
        total_despesas
    )

    percentual_pendentes = percentual_parte(
        total_pendentes,
        total_despesas
    )

    return render_template(
        "relatorios.html",
        meses=MESES,
        mes=mes,
        ano=ano,
        anos=obter_anos(ano),
        total_receitas=total_receitas,
        total_gerais=total_gerais,
        total_fixas=total_fixas,
        total_despesas=total_despesas,
        total_pagas=total_pagas,
        total_pendentes=total_pendentes,
        resultado_periodo=resultado_periodo,
        percentual_gerais=percentual_gerais,
        percentual_fixas=percentual_fixas,
        percentual_pagas=percentual_pagas,
        percentual_pendentes=percentual_pendentes,
        despesas_gerais_categoria=(
            despesas_gerais_categoria
        ),
        despesas_fixas_categoria=(
            despesas_fixas_categoria
        ),
        despesas_total_categoria=(
            despesas_total_categoria
        ),
        receitas_categoria=receitas_categoria,
    )


@app.route("/despesas")
def despesas():
    if not DB_PATH.exists():
        return render_template(
            "sem_banco.html",
            caminho=str(DB_PATH)
        )

    garantir_estrutura()

    mes, ano = obter_periodo()

    ordem = request.args.get("ordem", "desc").lower()
    if ordem not in {"asc", "desc"}:
        ordem = "desc"
    editar_id = request.args.get(
        "editar",
        type=int
    )

    despesa_edicao = None

    with conectar() as conexao:

        # A geração continua acontecendo normalmente,
        # mas esta página exibe somente as despesas manuais.
        gerar_despesas_fixas_do_periodo(
            conexao,
            mes,
            ano
        )

        itens = carregar_despesas(
            conexao,
            mes,
            ano,
            origem="gerais",
            ordem=ordem
        )

        if editar_id:

            registro = conexao.execute("""
                SELECT
                    id,
                    descricao,
                    valor,
                    categoria,
                    data,
                    COALESCE(pago, 1) AS pago,
                    despesa_fixa_id,
                    cartao_fatura_id
                FROM despesas
                WHERE id = ?
                  AND despesa_fixa_id IS NULL
                  AND cartao_fatura_id IS NULL
            """, (
                editar_id,
            )).fetchone()

            if registro:
                despesa_edicao = dict(
                    registro
                )

                despesa_edicao["data_html"] = (
                    converter_data_para_html(
                        despesa_edicao["data"]
                    )
                )

    total_gerais = sum(
        float(item["valor"] or 0)
        for item in itens
    )

    return render_template(
        "despesas.html",
        meses=MESES,
        mes=mes,
        ano=ano,
        anos=obter_anos(ano),
        ordem=ordem,
        categorias=obter_nomes_categorias("despesa"),
        despesas=itens,
        despesa_edicao=despesa_edicao,
        total_gerais=total_gerais,
        data_hoje=datetime.now().strftime(
            "%Y-%m-%d"
        ),
    )


@app.route("/despesas-fixas")
def despesas_fixas():
    if not DB_PATH.exists():
        return render_template(
            "sem_banco.html",
            caminho=str(DB_PATH)
        )

    garantir_estrutura()

    mes, ano = obter_periodo()

    ordem = request.args.get(
        "ordem",
        "asc"
    ).lower()

    if ordem not in {"asc", "desc"}:
        ordem = "asc"

    editar_id = request.args.get(
        "editar",
        type=int
    )

    cadastro_id = request.args.get(
        "cadastro",
        type=int
    )

    despesa_edicao = None
    cadastro_edicao = None

    with conectar() as conexao:

        gerar_despesas_fixas_do_periodo(
            conexao,
            mes,
            ano
        )

        itens = carregar_despesas(
            conexao,
            mes,
            ano,
            origem="fixas",
            ordem=ordem
        )

        cadastros_db = conexao.execute("""
            SELECT
                id,
                descricao,
                valor,
                categoria,
                dia_vencimento,
                ativa,
                data_inicio
            FROM despesas_fixas
            ORDER BY
                ativa DESC,
                dia_vencimento,
                descricao
        """).fetchall()

        cadastros = []

        for item in cadastros_db:
            cadastro = dict(item)
            cadastro["data_inicio_html"] = (
                converter_data_para_html(
                    cadastro["data_inicio"]
                )
            )
            cadastros.append(cadastro)

        if editar_id:

            registro = conexao.execute("""
                SELECT
                    id,
                    descricao,
                    valor,
                    categoria,
                    data,
                    COALESCE(pago, 1) AS pago,
                    despesa_fixa_id
                FROM despesas
                WHERE id = ?
                  AND despesa_fixa_id IS NOT NULL
            """, (
                editar_id,
            )).fetchone()

            if registro:
                despesa_edicao = dict(
                    registro
                )

                despesa_edicao["data_html"] = (
                    converter_data_para_html(
                        despesa_edicao["data"]
                    )
                )

        if cadastro_id:

            registro = conexao.execute("""
                SELECT
                    id,
                    descricao,
                    valor,
                    categoria,
                    dia_vencimento,
                    ativa,
                    data_inicio
                FROM despesas_fixas
                WHERE id = ?
            """, (
                cadastro_id,
            )).fetchone()

            if registro:
                cadastro_edicao = dict(
                    registro
                )

                cadastro_edicao["data_inicio_html"] = (
                    converter_data_para_html(
                        cadastro_edicao["data_inicio"]
                    )
                )

    total_fixas = sum(
        float(item["valor"] or 0)
        for item in itens
    )

    total_fixas_pagas = sum(
        float(item["valor"] or 0)
        for item in itens
        if item["pago"] == 1
    )

    total_fixas_pendentes = sum(
        float(item["valor"] or 0)
        for item in itens
        if item["pago"] == 0
    )

    return render_template(
        "despesas_fixas.html",
        meses=MESES,
        mes=mes,
        ano=ano,
        anos=obter_anos(ano),
        ordem=ordem,
        categorias=obter_nomes_categorias("despesa"),
        despesas=itens,
        despesa_edicao=despesa_edicao,
        cadastros=cadastros,
        cadastro_edicao=cadastro_edicao,
        total_fixas=total_fixas,
        total_fixas_pagas=total_fixas_pagas,
        total_fixas_pendentes=(
            total_fixas_pendentes
        ),
        data_hoje=datetime.now().strftime(
            "%Y-%m-%d"
        ),
    )


@app.post("/despesas-fixas/cadastro/salvar")
def salvar_cadastro_despesa_fixa():
    if not DB_PATH.exists():
        return redirect(
            url_for("dashboard")
        )

    garantir_estrutura()

    id_fixa = request.form.get(
        "id",
        type=int
    )

    descricao = request.form.get(
        "descricao",
        ""
    ).strip()

    valor_texto = request.form.get(
        "valor",
        ""
    ).strip()

    categoria = request.form.get(
        "categoria",
        ""
    ).strip()

    dia_texto = request.form.get(
        "dia_vencimento",
        ""
    ).strip()

    data_inicio_html = request.form.get(
        "data_inicio",
        ""
    ).strip()

    ativa_texto = request.form.get(
        "ativa",
        "1"
    )

    mes = request.form.get(
        "mes",
        datetime.now().strftime("%m")
    )

    ano = request.form.get(
        "ano",
        str(datetime.now().year)
    )

    if (
        not descricao
        or not valor_texto
        or not categoria
        or not dia_texto
        or not data_inicio_html
    ):
        flash(
            "Preencha todos os campos da despesa fixa.",
            "erro"
        )
        return redirect(
            url_for(
                "despesas_fixas",
                mes=mes,
                ano=ano,
                cadastro=id_fixa
            )
        )

    try:
        valor = float(
            valor_texto
            .replace(".", "")
            .replace(",", ".")
        )
    except ValueError:
        flash(
            "Digite um valor válido.",
            "erro"
        )
        return redirect(
            url_for(
                "despesas_fixas",
                mes=mes,
                ano=ano,
                cadastro=id_fixa
            )
        )

    if valor < 0:
        flash(
            "O valor não pode ser negativo.",
            "erro"
        )
        return redirect(
            url_for(
                "despesas_fixas",
                mes=mes,
                ano=ano,
                cadastro=id_fixa
            )
        )

    try:
        dia_vencimento = int(
            dia_texto
        )
    except ValueError:
        dia_vencimento = 0

    if dia_vencimento < 1 or dia_vencimento > 31:
        flash(
            "O dia do vencimento deve estar entre 1 e 31.",
            "erro"
        )
        return redirect(
            url_for(
                "despesas_fixas",
                mes=mes,
                ano=ano,
                cadastro=id_fixa
            )
        )

    data_inicio = converter_data_para_banco(
        data_inicio_html
    )

    if not data_inicio:
        flash(
            "Digite uma data de início válida.",
            "erro"
        )
        return redirect(
            url_for(
                "despesas_fixas",
                mes=mes,
                ano=ano,
                cadastro=id_fixa
            )
        )

    ativa = (
        1
        if ativa_texto == "1"
        else 0
    )

    with conectar() as conexao:

        if id_fixa:

            existente = conexao.execute("""
                SELECT id
                FROM despesas_fixas
                WHERE id = ?
            """, (
                id_fixa,
            )).fetchone()

            if not existente:
                flash(
                    "Cadastro de despesa fixa não encontrado.",
                    "erro"
                )

            else:
                conexao.execute("""
                    UPDATE despesas_fixas
                    SET descricao = ?,
                        valor = ?,
                        categoria = ?,
                        dia_vencimento = ?,
                        ativa = ?,
                        data_inicio = ?
                    WHERE id = ?
                """, (
                    descricao,
                    valor,
                    categoria,
                    dia_vencimento,
                    ativa,
                    data_inicio,
                    id_fixa
                ))

                conexao.commit()

                flash(
                    "Cadastro da despesa fixa alterado. "
                    "Cobranças já geradas não foram modificadas.",
                    "sucesso"
                )

        else:

            conexao.execute("""
                INSERT INTO despesas_fixas (
                    descricao,
                    valor,
                    categoria,
                    dia_vencimento,
                    ativa,
                    data_inicio
                )
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                descricao,
                valor,
                categoria,
                dia_vencimento,
                ativa,
                data_inicio
            ))

            conexao.commit()

            flash(
                "Despesa fixa cadastrada com sucesso.",
                "sucesso"
            )

    return redirect(
        url_for(
            "despesas_fixas",
            mes=mes,
            ano=ano
        )
    )


@app.post("/despesas-fixas/cadastro/<int:id_fixa>/situacao")
def alterar_situacao_despesa_fixa(id_fixa):
    mes = request.form.get(
        "mes",
        datetime.now().strftime("%m")
    )

    ano = request.form.get(
        "ano",
        str(datetime.now().year)
    )

    acao = request.form.get(
        "acao",
        ""
    )

    if acao not in {"ativar", "inativar"}:
        flash(
            "Ação inválida.",
            "erro"
        )
        return redirect(
            url_for(
                "despesas_fixas",
                mes=mes,
                ano=ano
            )
        )

    ativa = 1 if acao == "ativar" else 0

    with conectar() as conexao:
        conexao.execute("""
            UPDATE despesas_fixas
            SET ativa = ?
            WHERE id = ?
        """, (
            ativa,
            id_fixa
        ))

        conexao.commit()

    flash(
        "Despesa fixa ativada."
        if ativa
        else "Despesa fixa inativada. Novas cobranças não serão geradas enquanto ela estiver inativa.",
        "sucesso"
    )

    return redirect(
        url_for(
            "despesas_fixas",
            mes=mes,
            ano=ano
        )
    )


@app.post("/despesas-fixas/cadastro/<int:id_fixa>/excluir")
def excluir_cadastro_despesa_fixa(id_fixa):
    mes = request.form.get(
        "mes",
        datetime.now().strftime("%m")
    )

    ano = request.form.get(
        "ano",
        str(datetime.now().year)
    )

    with conectar() as conexao:
        existente = conexao.execute("""
            SELECT id
            FROM despesas_fixas
            WHERE id = ?
        """, (
            id_fixa,
        )).fetchone()

        if not existente:
            flash(
                "Cadastro de despesa fixa não encontrado.",
                "erro"
            )

            return redirect(
                url_for(
                    "despesas_fixas",
                    mes=mes,
                    ano=ano
                )
            )

        # As cobranças já geradas mantêm despesa_fixa_id.
        # Assim continuam classificadas como despesas fixas
        # no histórico, mesmo após a exclusão do cadastro.
        conexao.execute("""
            DELETE FROM despesas_fixas_excecoes
            WHERE despesa_fixa_id = ?
        """, (
            id_fixa,
        ))

        conexao.execute("""
            DELETE FROM despesas_fixas
            WHERE id = ?
        """, (
            id_fixa,
        ))

        conexao.commit()

    flash(
        "Cadastro da despesa fixa excluído. "
        "As cobranças já geradas foram preservadas no histórico.",
        "sucesso"
    )

    return redirect(
        url_for(
            "despesas_fixas",
            mes=mes,
            ano=ano
        )
    )


@app.post("/despesas/salvar")
def salvar_despesa():
    if not DB_PATH.exists():
        return redirect(url_for("dashboard"))

    garantir_estrutura()

    id_despesa = request.form.get("id", type=int)
    descricao = request.form.get("descricao", "").strip()
    valor_texto = request.form.get("valor", "").strip()
    categoria = request.form.get("categoria", "").strip()
    data_html = request.form.get("data", "").strip()
    pago = 1 if request.form.get("status") == "pago" else 0

    mes_retorno = request.form.get(
        "mes_retorno",
        datetime.now().strftime("%m")
    )

    ano_retorno = request.form.get(
        "ano_retorno",
        str(datetime.now().year)
    )

    pagina_retorno = request.form.get(
        "pagina_retorno",
        "gerais"
    )

    if not descricao or not valor_texto or not categoria or not data_html:
        flash("Preencha todos os campos.", "erro")
        return redirecionar_pagina_despesas(
            pagina_retorno,
            mes_retorno,
            ano_retorno
        )

    try:
        valor = float(valor_texto.replace(".", "").replace(",", "."))
    except ValueError:
        flash("Digite um valor válido.", "erro")
        return redirecionar_pagina_despesas(
            pagina_retorno,
            mes_retorno,
            ano_retorno
        )

    data_banco = converter_data_para_banco(data_html)

    if not data_banco:
        flash("Digite uma data válida.", "erro")
        return redirecionar_pagina_despesas(
            pagina_retorno,
            mes_retorno,
            ano_retorno
        )

    data_pagamento = datetime.now().strftime("%d/%m/%Y") if pago else None

    with conectar() as conexao:
        if id_despesa:
            existente = conexao.execute("""
                SELECT
                    id,
                    cartao_fatura_id
                FROM despesas
                WHERE id = ?
            """, (id_despesa,)).fetchone()

            if not existente:
                flash("Despesa não encontrada.", "erro")
            elif existente["cartao_fatura_id"] is not None:
                flash(
                    "A fatura do cartão deve ser alterada pela tela Cartão de Crédito.",
                    "erro"
                )
                return redirect(url_for("cartao"))
            else:
                conexao.execute("""
                    UPDATE despesas
                    SET descricao = ?,
                        valor = ?,
                        categoria = ?,
                        data = ?,
                        pago = ?,
                        data_pagamento = ?
                    WHERE id = ?
                """, (
                    descricao,
                    valor,
                    categoria,
                    data_banco,
                    pago,
                    data_pagamento,
                    id_despesa
                ))
                conexao.commit()
                flash("Despesa alterada com sucesso.", "sucesso")
        else:
            conexao.execute("""
                INSERT INTO despesas (
                    descricao,
                    valor,
                    categoria,
                    data,
                    pago,
                    data_pagamento,
                    despesa_fixa_id
                )
                VALUES (?, ?, ?, ?, ?, ?, NULL)
            """, (
                descricao,
                valor,
                categoria,
                data_banco,
                pago,
                data_pagamento
            ))
            conexao.commit()
            flash("Despesa adicionada com sucesso.", "sucesso")

    mes_destino = data_banco[3:5]
    ano_destino = data_banco[6:10]

    return redirecionar_pagina_despesas(
        pagina_retorno,
        mes_destino,
        ano_destino
    )


@app.post("/despesas/<int:id_despesa>/status")
def alterar_status_despesa(id_despesa):
    status = request.form.get("status")
    mes = request.form.get("mes", datetime.now().strftime("%m"))
    ano = request.form.get(
        "ano",
        str(datetime.now().year)
    )

    pagina = request.form.get(
        "pagina",
        "gerais"
    )

    if status not in {"pago", "pendente"}:
        flash(
            "Status inválido.",
            "erro"
        )

        return redirecionar_pagina_despesas(
            pagina,
            mes,
            ano
        )

    pago = 1 if status == "pago" else 0
    data_pagamento = datetime.now().strftime("%d/%m/%Y") if pago else None

    with conectar() as conexao:
        item = conexao.execute("""
            SELECT cartao_fatura_id
            FROM despesas
            WHERE id = ?
        """, (id_despesa,)).fetchone()

        conexao.execute("""
            UPDATE despesas
            SET pago = ?,
                data_pagamento = ?
            WHERE id = ?
        """, (
            pago,
            data_pagamento,
            id_despesa
        ))

        if item and item["cartao_fatura_id"] is not None:
            conexao.execute("""
                UPDATE cartao_faturas
                SET pago = ?,
                    data_pagamento = ?
                WHERE id = ?
            """, (
                pago,
                data_pagamento,
                item["cartao_fatura_id"]
            ))

        conexao.commit()

    flash(
        "Despesa marcada como paga."
        if pago
        else "Despesa marcada como pendente.",
        "sucesso"
    )

    return redirecionar_pagina_despesas(
        pagina,
        mes,
        ano
    )


@app.post("/despesas/<int:id_despesa>/excluir")
def excluir_despesa(id_despesa):
    mes = request.form.get("mes", datetime.now().strftime("%m"))
    ano = request.form.get(
        "ano",
        str(datetime.now().year)
    )

    pagina = request.form.get(
        "pagina",
        "gerais"
    )

    with conectar() as conexao:
        item = conexao.execute("""
            SELECT
                despesa_fixa_id,
                cartao_fatura_id,
                data
            FROM despesas
            WHERE id = ?
        """, (id_despesa,)).fetchone()

        if not item:
            flash("Despesa não encontrada.", "erro")
            return redirecionar_pagina_despesas(
                pagina,
                mes,
                ano
            )

        if item["cartao_fatura_id"] is not None:
            flash(
                "A fatura do cartão deve ser gerenciada pela tela Cartão de Crédito.",
                "erro"
            )
            return redirect(
                url_for(
                    "cartao",
                    fatura=item["cartao_fatura_id"]
                )
            )

        if item["despesa_fixa_id"] is not None and item["data"]:
            try:
                data_obj = datetime.strptime(item["data"], "%d/%m/%Y")
                competencia = data_obj.strftime("%Y-%m")

                conexao.execute("""
                    INSERT OR IGNORE INTO despesas_fixas_excecoes (
                        despesa_fixa_id,
                        competencia
                    )
                    VALUES (?, ?)
                """, (
                    item["despesa_fixa_id"],
                    competencia
                ))
            except ValueError:
                pass

        conexao.execute("""
            DELETE FROM despesas
            WHERE id = ?
        """, (id_despesa,))
        conexao.commit()

    flash(
        "Despesa excluída deste mês.",
        "sucesso"
    )

    return redirecionar_pagina_despesas(
        pagina,
        mes,
        ano
    )


@app.route("/receitas")
def receitas():
    if not DB_PATH.exists():
        return render_template("sem_banco.html", caminho=str(DB_PATH))

    garantir_estrutura()
    mes, ano = obter_periodo()

    ordem = request.args.get("ordem", "desc").lower()
    if ordem not in {"asc", "desc"}:
        ordem = "desc"
    editar_id = request.args.get("editar", type=int)
    receita_edicao = None

    with conectar() as conexao:
        itens = conexao.execute("""
            SELECT
                id,
                descricao,
                valor,
                categoria,
                data
            FROM receitas
            WHERE substr(data, 4, 2) = ?
              AND substr(data, 7, 4) = ?
            ORDER BY CAST(substr(data, 1, 2) AS INTEGER) DESC, id DESC
        """, (mes, ano)).fetchall()

        if editar_id:
            registro = conexao.execute("""
                SELECT
                    id,
                    descricao,
                    valor,
                    categoria,
                    data
                FROM receitas
                WHERE id = ?
            """, (editar_id,)).fetchone()

            if registro:
                receita_edicao = dict(registro)
                receita_edicao["data_html"] = converter_data_para_html(
                    receita_edicao["data"]
                )

    if ordem == "asc":
        itens = list(reversed(itens))

    total_receitas = sum(float(item["valor"] or 0) for item in itens)

    return render_template(
        "receitas.html",
        meses=MESES,
        mes=mes,
        ano=ano,
        anos=obter_anos(ano),
        ordem=ordem,
        categorias=obter_nomes_categorias("receita"),
        receitas=itens,
        receita_edicao=receita_edicao,
        total_receitas=total_receitas,
        data_hoje=datetime.now().strftime("%Y-%m-%d"),
    )


@app.post("/receitas/salvar")
def salvar_receita():
    if not DB_PATH.exists():
        return redirect(url_for("dashboard"))

    garantir_estrutura()

    id_receita = request.form.get("id", type=int)
    descricao = request.form.get("descricao", "").strip()
    valor_texto = request.form.get("valor", "").strip()
    categoria = request.form.get("categoria", "").strip()
    data_html = request.form.get("data", "").strip()

    mes_retorno = request.form.get(
        "mes_retorno",
        datetime.now().strftime("%m")
    )
    ano_retorno = request.form.get(
        "ano_retorno",
        str(datetime.now().year)
    )

    if not descricao or not valor_texto or not categoria or not data_html:
        flash("Preencha todos os campos.", "erro")
        return redirect(
            url_for(
                "receitas",
                mes=mes_retorno,
                ano=ano_retorno
            )
        )

    try:
        valor = float(
            valor_texto.replace(".", "").replace(",", ".")
        )
    except ValueError:
        flash("Digite um valor válido.", "erro")
        return redirect(
            url_for(
                "receitas",
                mes=mes_retorno,
                ano=ano_retorno
            )
        )

    if valor < 0:
        flash("O valor da receita não pode ser negativo.", "erro")
        return redirect(
            url_for(
                "receitas",
                mes=mes_retorno,
                ano=ano_retorno
            )
        )

    data_banco = converter_data_para_banco(data_html)

    if not data_banco:
        flash("Digite uma data válida.", "erro")
        return redirect(
            url_for(
                "receitas",
                mes=mes_retorno,
                ano=ano_retorno
            )
        )

    with conectar() as conexao:

        if id_receita:

            existente = conexao.execute("""
                SELECT id
                FROM receitas
                WHERE id = ?
            """, (id_receita,)).fetchone()

            if not existente:
                flash("Receita não encontrada.", "erro")

            else:
                conexao.execute("""
                    UPDATE receitas
                    SET descricao = ?,
                        valor = ?,
                        categoria = ?,
                        data = ?
                    WHERE id = ?
                """, (
                    descricao,
                    valor,
                    categoria,
                    data_banco,
                    id_receita
                ))

                conexao.commit()

                flash(
                    "Receita alterada com sucesso.",
                    "sucesso"
                )

        else:

            conexao.execute("""
                INSERT INTO receitas (
                    descricao,
                    valor,
                    categoria,
                    data
                )
                VALUES (?, ?, ?, ?)
            """, (
                descricao,
                valor,
                categoria,
                data_banco
            ))

            conexao.commit()

            flash(
                "Receita adicionada com sucesso.",
                "sucesso"
            )

    mes_destino = data_banco[3:5]
    ano_destino = data_banco[6:10]

    return redirect(
        url_for(
            "receitas",
            mes=mes_destino,
            ano=ano_destino
        )
    )


@app.post("/receitas/<int:id_receita>/excluir")
def excluir_receita(id_receita):

    mes = request.form.get(
        "mes",
        datetime.now().strftime("%m")
    )
    ano = request.form.get(
        "ano",
        str(datetime.now().year)
    )

    with conectar() as conexao:

        existente = conexao.execute("""
            SELECT id
            FROM receitas
            WHERE id = ?
        """, (id_receita,)).fetchone()

        if not existente:
            flash(
                "Receita não encontrada.",
                "erro"
            )

            return redirect(
                url_for(
                    "receitas",
                    mes=mes,
                    ano=ano
                )
            )

        conexao.execute("""
            DELETE FROM receitas
            WHERE id = ?
        """, (
            id_receita,
        ))

        conexao.commit()

    flash(
        "Receita excluída com sucesso.",
        "sucesso"
    )

    return redirect(
        url_for(
            "receitas",
            mes=mes,
            ano=ano
        )
    )



if __name__ == "__main__":
    garantir_estrutura()
    criar_backup_automatico()
    print()
    print("Controle Financeiro Web")
    print("Abra no navegador: http://127.0.0.1:5000")
    print()
    app.run(host="0.0.0.0", port=5000, debug=True)
