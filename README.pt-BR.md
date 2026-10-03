<div align="center">

<img src="assets/banner.svg" alt="poordjaevin" width="100%"/>

</div>

<h1 align="center">poordjaevin</h1>

<p align="center"><b>O Jev do pobre.</b> Uma camada de decisão "Sistema Um" open source e local-first para apps de LLM: decisões tipadas com <b>confiança calibrada e comprovada</b>. Sem API key. Sem waitlist.</p>

> **Nota do fork:** este é o fork do ecossistema Devin de
> [rupeshpoojary9/poordjaevin](https://github.com/rupeshpoojary9/poordjaevin).
> Adiciona um **backend Devin ACP**: `poordjaevin serve` pontua através do
> modelo que o teu Devin CLI já usa, com rotação automática de modelo e
> telemetria de custo por chamada. Ou seja, para utilizadores Devin: sem
> download de modelo extra, sem Ollama, sem VM, sem API key além das
> credenciais do próprio Devin. O backend local original sem chave continua
> disponível como fallback totalmente offline (`POORDJAEVIN_BACKEND=nli`).

[English version](README.md)

## O que é

A `0.9` do teu modelo é um palpite. A `0.9` do poordjaevin é uma medida.

Todo LLM em modo JSON devolve uma confiança e espera que não verifiques. O
poordjaevin verifica: no eval set incluído, corta o erro de calibração (ECE)
de **0.170 para 0.071** sem perda de acurácia. Serve para as decisões rápidas
e estruturadas do dia a dia: rotear um ticket, classificar intenção, pontuar
sentimento, barrar uma tool call arriscada.

## Instalação

Ainda não está no PyPI. Instala do GitHub:

```bash
# Linux / macOS
pip install "poordjaevin[local] @ git+https://github.com/Icaro0310/poordjaevin.git"
```

```powershell
# Windows (PowerShell)
py -m pip install "poordjaevin[local] @ git+https://github.com/Icaro0310/poordjaevin.git"
```

`[local]` instala torch + transformers para o backend NLI offline. Se vais
usar apenas o backend ACP do Devin (default do `serve`), basta
`pip install "poordjaevin @ git+https://github.com/Icaro0310/poordjaevin.git"`.

## Uso com Devin (servidor MCP, só-Devin)

**Este modo não precisa de nada além do Devin.** O backend default (`acp`)
fala com `devin acp` através de uma ponte Node incluída no pacote: cada
decisão é pontuada pelo modelo que o teu plano Devin já fornece, com rotação
automática. Sem Ollama, sem VM, sem túnel, sem segunda API key — a ponte lê o
mesmo `credentials.toml` que o Devin CLI usa.

```bash
pipx install "poordjaevin[mcp] @ git+https://github.com/Icaro0310/poordjaevin.git"
```

Configuração MCP do Devin:

```json
{
  "mcpServers": {
    "poordjaevin": { "command": "poordjaevin", "args": ["serve"] }
  }
}
```

Requisitos do backend ACP: `devin` no PATH (ou `DEVIN_CLI_PATH`), Node.js >= 18,
e credenciais Devin válidas em `%APPDATA%\devin\credentials.toml` (Windows) ou
`~/.local/share/devin/credentials.toml` (Linux, sobrescreve com
`DEVIN_CREDENTIALS_PATH`).

Variáveis de ambiente (todas opcionais):

| Variável | Default | Significado |
|---|---|---|
| `POORDJAEVIN_BACKEND` | `acp` | `acp` = modelo do Devin via ACP; `nli` = modelo local offline |
| `POORDJAEVIN_ACP_MODEL` | auto | fixa um modelo Devin em vez da rotação automática |
| `POORDJAEVIN_ACP_TIMEOUT` | `120` | segundos por decisão |
| `POORDJAEVIN_ACP_MAX_COST` | unset | falha fechada quando o custo ACP acumulado passa deste teto |
| `POORDJAEVIN_ABSTAIN` | `off` | `on` = abstém-se abaixo do limiar calibrado |

Nota de honestidade: com `acp`, a confiança é `self_report` (probabilidade
declarada pelo próprio modelo, ajustada por temperatura), não logprobs de NLI.
Cada resposta leva `confidence_source` para nunca confundir os dois, e
`model`/`cost` são registados por chamada.

Sem Devin na máquina? Caminho offline:

```bash
pipx install "poordjaevin[local,mcp] @ git+https://github.com/Icaro0310/poordjaevin.git"
POORDJAEVIN_BACKEND=nli poordjaevin serve   # download único de ~400MB, depois offline
```

Também funciona com Claude Code/Desktop (`claude mcp add poordjaevin --
poordjaevin serve`).

## Tools expostas

| Tool | O que faz |
|---|---|
| `gate(action)` | guardrail: esta ação deve ser bloqueada (mexe em dinheiro, apaga dados)? |
| `judge(text, statement)` | pergunta sim/não com `P(true)` calibrada |
| `classify(text, options)` | escolhe uma opção, com confiança calibrada |
| `rate(text, levels)` | score ordinal (baixo / médio / alto) |
| `decide(text, questions)` | várias perguntas tipadas numa passagem |

## Os três primitivos

| Primitivo | Uso | Retorna |
|---|---|---|
| `Choice(options)` | classificação, routing | opção vencedora, probabilidades, confiança calibrada |
| `Score(levels)` | rating ordinal, severidade | nível vencedor, score contínuo, confiança |
| `Noul(statement)` | gates sim/não, guardrails | `P(true)` com threshold |

O `value` retornado sai **sempre** do conjunto declarado — categoria inválida
é impossível por construção, não "normalmente evitada".

## Suporte de plataforma

Windows, Linux e macOS. A ponte ACP resolve credenciais e o executável `devin`
por plataforma:

| Plataforma | Credenciais Devin | Devin CLI |
|---|---|---|
| Windows | `%APPDATA%\devin\credentials.toml` | `devin.exe` no PATH |
| Linux | `$XDG_DATA_HOME/devin/credentials.toml` (default `~/.local/share/devin/credentials.toml`) | `devin` no PATH |
| macOS | `~/Library/Application Support/devin/credentials.toml` | `devin` no PATH |

Sobrescreve com `DEVIN_CREDENTIALS_PATH` e `DEVIN_CLI_PATH`. O ficheiro de
credenciais só é lido para autenticar a sessão ACP — nunca é logado nem copiado.

## Benchmarks e limitações honestas

```bash
poordjaevin eval       --set evalset/tasks.jsonl          # acurácia, ECE, Brier
poordjaevin calibrate  --set evalset/tasks.jsonl --plots  # ECE antes/depois
```

No eval set incluído (55 itens, 160 decisões, backend NLI local): acurácia
0.781, ECE 0.170 → 0.071 com temperatura. Resultados completos e benchmark
cruzado contra Jev, Laya e von em [RESULTS.md](RESULTS.md) e
[`crossbench/`](crossbench/) — o poordjaevin lidera as opções locais abertas
no benchmark multi-primitivo, mas `von` vence em classificação de alta
cardinalidade e Jev vence no geral. Sem hype: os números são reproduzíveis e
as limitações estão documentadas no README em inglês.

## Licença

MIT. Usa, distribui, vende.


---

Se isso te poupou tempo de depuração, uma ⭐ no repositório ajuda outras pessoas a encontrá-lo.
