# omie-webhook

## Fila automatica do portal (sem confirmacao manual no Render)

O Web Service consome automaticamente as solicitacoes explicitamente colocadas
na fila do portal. Nao depende mais de concluir a conferencia de todos os pedidos.
A fila vazia consulta somente o portal a cada 30 segundos, sem chamar a Omie.
O endpoint autenticado `POST /portal/ativa/processar-fila` antecipa essa verificacao
quando o portal registra um novo reenvio. Ele responde 202, nao uma confirmacao de inclusao.

Use `ATIVA_PORTAL_URL` e `ATIVA_BRIDGE_TOKEN` ja configurados no Web Service.
`ATIVA_QUEUE_ENABLED=0` desativa o consumidor, se necessario. O Gunicorn inicia
o consumidor em `post_worker_init`; existe tambem inicializacao na primeira
requisicao para outros comandos de inicio. Nenhum novo servico pago e criado.

Cada tentativa confere a ausencia na 004 antes de incluir, usa o codigo
de integracao com sufixo `-ATIVA` e publica erro ou confirmacao com o numero
de destino. Se o retorno ao portal falhar, repete somente a entrega do retorno,
nunca a inclusao. Uma falha de negocio exige corrigir o motivo e solicitar nova
tentativa no portal. A conferencia periodica continua como verificacao adicional.

As transferencias deste processo sao sequenciais. Chamadas Omie deste processo
tem intervalo minimo de 1,1 segundo; leituras identicas aguardam 30 segundos.
Outros processos e integracoes com a mesma chave precisam respeitar os limites
tambem: esse controle local nao representa um limite global de todos os consumidores.

Validacao local, sem chamadas reais: `python -m unittest -v test_fila_portal.py`.

## Protecao contra reenvio infinito e bloqueio da Omie

- Pedido que a 001 informa como "nao cadastrado" (excluido ou de outra empresa)
  responde 200 ao webhook: repetir nao resolve e so consome a cota da Omie.
- Evento com `appKey` diferente da 001 e ignorado sem chamar a Omie.
- Quando a Omie informa "API bloqueada por consumo indevido", o processo suspende
  todas as chamadas pelo prazo informado (mais 30 segundos). Nesse periodo o
  webhook responde 503 sem consultar a Omie, e a Omie reenvia o evento depois.
- Validacao local: `python -m unittest -v test_webhook_protecao.py`.

## Conferencia dos pedidos 001 para 004

O Cron Job separado do webhook esta definido em `render.yaml`. No Render,
crie um Blueprint com este repositorio e cadastre as quatro variaveis de
credenciais das duas empresas. A rotina roda aos minutos 07 e 37 de cada hora.

Ela consulta pedidos emitidos nos ultimos sete dias corridos (incluindo hoje,
no horario de Brasilia) que estao na etapa 10/00 da 001 e verifica a UF do
cadastro. Para ES e MG, procura a copia pelo codigo com sufixo `-ATIVA`.
Clientes da lista `CLIENTES_EXCLUIDOS` (padrao: MAXMIX, ESTOK, VIVARA, CIELLE,
buscados no nome fantasia e na razao social, sem diferenciar acento, espaco ou
pontuacao) nao sao replicados pelo webhook nem contam como pendencia aqui.
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

## Envio da conferencia e reenvio pelo portal

O `gunicorn.conf.py` usa um processo com quatro threads (`gthread`). Isso
mantem o heartbeat do processo e as rotas de saude/sinalizacao disponiveis
durante consultas demoradas ao Omie. O modo `sync` encerrava o processo apos
120 segundos de um webhook, interrompendo tambem a fila e apagando caches.
Os locks de transferencia e de chamadas Omie continuam serializando as
operacoes; quatro threads HTTP nao significam quatro inclusoes simultaneas.
O comando existente do Render carrega este arquivo automaticamente a partir
da raiz. Nao acrescente `--worker-class sync`. As pausas de consulta sao
centralizadas em `chamar_omie`, sem pausa duplicada em referencias.

Configure `ATIVA_PORTAL_URL` com a URL do portal e `ATIVA_BRIDGE_TOKEN`
com o mesmo segredo dedicado no Worker Cloudflare, no Cron Job
`omie-ativa-conferencia` e no Web Service `Replicador-Pedidos-Ativa`.
O Worker deve receber o segredo antes de ativar o envio. Configure a URL
nas duas tarefas do Render. Sem essas variaveis a conferencia e o webhook
continuam rodando com os logs locais, sem trocar dados com o portal.

O Cron Job publica somente a conferencia completa. Se uma consulta falhar,
a tela conserva o ultimo resultado e sinaliza a falha. Ele processa ate um
reenvio autorizado por rodada e pede ao replicador para confirmar o pedido
na ATIVA antes de qualquer nova inclusao. Uma resposta inconclusiva fica
registrada na tentativa e exige nova verificacao antes de reenviar.
