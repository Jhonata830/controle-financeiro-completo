# Controle Financeiro 2 — Modo Mercado Pago

Ajustes implementados:

- 100% do CDI configurável;
- CDI anual sugerido em 13,90% quando a Selic alvo está em 14%;
- 252 dias úteis;
- novos depósitos/Receitas entram na base apenas no próximo dia útil;
- IR regressivo automático: 22,5%, 20%, 17,5% e 15%;
- IOF não aplicado;
- histórico mostra bruto, IR e líquido;
- Dashboard usa o rendimento líquido;
- botão Recalcular histórico para corrigir registros da versão anterior.

Importante: o cálculo é uma aproximação. O extrato real do Mercado Pago pode
considerar horário de entrada, saldo efetivamente disponível, arredondamentos
e lotes internos que o app não conhece.

Preserve na atualização:
- dados/despesas.db
- backups/
- .env

Container: controle-financeiro-2
Porta: 5051
