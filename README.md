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
