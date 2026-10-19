# Controle Financeiro 2 — Mercado Pago

Aplicação web pessoal de controle financeiro construída com Python, Flask, SQLite, HTML, CSS e JavaScript. Esta edição inclui o módulo de rendimento estimado inspirado no rendimento do Mercado Pago e o gerenciamento de cartões de crédito.

## Recursos

- Cadastro e acompanhamento de despesas e receitas.
- Despesas fixas e recorrências.
- Categorias personalizáveis, relatórios e exportação CSV/ZIP.
- Backups automáticos do banco SQLite.
- Rendimento diário estimado baseado em percentual do CDI, com processamento de dias úteis.
- Estimativa de IR regressivo e histórico de rendimentos.
- Cartões múltiplos, faturas, compras à vista/parceladas e cobranças recorrentes.
- Interface responsiva para desktop e dispositivos móveis.

> **Aviso sobre o rendimento:** os valores são estimativas, não uma integração oficial com o Mercado Pago nem uma promessa de rentabilidade. O saldo real, horários de crédito, regras do produto, tributação e arredondamentos podem diferir. Confira sempre o extrato oficial e os termos atuais do serviço.

## Executar com Docker Compose

1. Instale Docker e Docker Compose.
2. Copie `.env.example` para `.env` e defina uma `SECRET_KEY` forte e exclusiva.
3. Crie as pastas `dados/` e `backups/` (elas já estão incluídas com `.gitkeep`).
4. Execute:

   ```bash
   docker compose up -d --build
   ```

5. Acesse `http://localhost:5051`.

O banco será criado em `dados/despesas.db`; os backups ficam em `backups/`. Esses arquivos são dados locais e não devem ser enviados ao GitHub.

## Executar localmente com Python

Requer Python 3.11+ (o projeto foi preparado para Python 3.13).

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# Linux/macOS:
# source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

Depois, abra o endereço local informado pelo Flask no terminal. Para uma instalação real, configure uma `SECRET_KEY` segura por variável de ambiente e use um servidor WSGI como Gunicorn.

## Configuração e privacidade

- `SECRET_KEY`: chave de sessão da aplicação; nunca publique o `.env`.
- `DATA_DIR`: diretório do banco SQLite (Docker usa `/app/dados`).
- `BACKUP_DIR`: diretório de backups (Docker usa `/app/backups`).
- O `.gitignore` exclui bancos, backups, ambientes virtuais e segredos.
- Não coloque neste repositório seu banco real, backups, tokens, senhas, endereço/IP privado ou configurações pessoais do Tailscale.

## Estrutura principal

- `app.py`: rotas e lógica da aplicação.
- `templates/`: páginas HTML.
- `static/`: CSS e JavaScript.
- `Dockerfile` e `docker-compose.yml`: execução em contêiner.
- `requirements.txt`: dependências Python.

## Licença

Nenhuma licença foi definida neste repositório. Se você quiser permitir reutilização por outras pessoas, escolha e adicione uma licença antes de publicar.
