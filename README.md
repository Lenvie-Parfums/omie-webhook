# omie-webhook

## Conferencia dos pedidos 001 para 004

O Cron Job separado do webhook esta definido em `render.yaml`. No Render,
crie um Blueprint com este repositorio e cadastre as quatro variaveis de
credenciais das duas empresas. A rotina roda aos minutos 07 e 37 de cada hora.

Ela consulta pedidos emitidos nos ultimos sete dias corridos (incluindo hoje,
no horario de Brasilia) que estao na etapa 10/00 da 001 e verifica a UF do
cadastro. Para ES e MG, procura a copia pelo codigo com sufixo `-ATIVA`.
Nos logs de Runs, cada linha mostra o status, o nome do cliente, a UF e os
numeros dos pedidos. Uma pendencia indica que a copia nao apareceu na listagem
da 004 dentro do periodo consultado. A rotina pagina as duas listagens, cacheia
os clientes por execucao e faz uma chamada de cada vez, com intervalo minimo
de 1,1 segundo. Ela termina com codigo 1 se encontrar pendencias ou erros e
interrompe a consulta quando a API rejeita uma chamada.
A rotina apenas consulta a Omie. Para auditar uma janela maior, execute
`python -u conferir_pedidos.py --dias 30` com as mesmas variaveis de ambiente.


## Dados enviados nos novos pedidos 001 para 004

Quando a Omie informa a lista de parcelas, a replicacao usa a condicao
personalizada 999 para enviar vencimentos, valores e meios de pagamento do
pedido da 001. Uma condicao sem parcelas detalhadas precisa de um de-para
para a 004, salvo a condicao a vista 000 ja usada pelas duas empresas.

Vendedor e transportadora sao procurados na 004 por nome/e-mail e por
CNPJ/CPF, respectivamente. Quando o cadastro nao tem correspondencia unica,
o nome de origem fica nas observacoes do pedido. As consultas de cadastro
sao sequenciais e os resultados ficam em cache.

O valor opcional de `OMIE_DE_PARA_004` e um objeto JSON indexado por grupo
e codigo de origem. Os grupos aceitos sao `parcelas`, `vendedores`,
`transportadoras`, `categorias`, `contas_correntes`, `projetos`,
`departamentos`, `locais_estoque` e `cenarios_fiscais`. Exemplo com
codigos ilustrativos:

```json
{"vendedores":{"1001":2001},"parcelas":{"003":"015"},"contas_correntes":{"1002":2002}}
```

Use somente codigos conferidos na 004. A filial conserva a categoria e
conta corrente padrao quando nao existe de-para. O rateio por departamento
so e enviado quando todos os departamentos do pedido tem equivalente.
Pedidos ja existentes na 004 continuam protegidos contra nova inclusao.
