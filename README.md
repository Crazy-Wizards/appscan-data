# appscan-data

Pacotes de dados diários do app AppScan (Magic: The Gathering). Um GitHub Action roda todo dia às 22:30 UTC.
Ele baixa as fontes públicas, junta tudo pelo Scryfall id e publica arquivos pequenos que o app baixa.

## Arquivos (release `data`)

URL fixa:

```
https://github.com/<owner>/appscan-data/releases/download/data/<arquivo>
```

| Arquivo | Conteúdo | Tamanho aprox. |
|---|---|---|
| `manifest.json` | versão/hash/tamanho de cada pacote + câmbio | 1 KB |
| `catalog_mtg.sqlite.gz` | catálogo SQLite (cartas, sets, índice FTS5) | 25 MB |
| `prices_mtg.jsonl.gz` | preços do dia por carta/fonte/acabamento | 5 MB |

O app baixa o `manifest.json` e só baixa o catálogo quando `catalog.version` mudar. Os preços ele baixa uma vez por dia.
Os snapshots diários ficam nas releases `prices-AAAA-MM-DD` (pre-release, guarda 7 dias).

## Formato de preços

O arquivo é JSONL comprimido com gzip. A linha 1 é o cabeçalho e, a partir dela, vem uma carta por linha:

```jsonc
{"date":"2026-10-13","fx":{"base":"USD","date":"2026-10-13","rates":{"BRL":5.0165,"EUR":0.894,"USD":1.0}},"game":"mtg","sources":{...},"v":1}
{"id":"<scryfall_id>","p":{"cardkingdom":{"n":{"r":3.49,"b":1.3,"nm":3.49,"ex":2.79,"vg":2.44,"g":1.75}},"cardmarket":{"n":{"r":2.01,"low":1.3,"avg":1.83}},"tcgplayer":{"n":{"r":1.26}}}}
```

- Acabamento: `n` normal, `f` foil, `e` etched.
- Campos:
  - `r`: valor usado para avaliar a coleção (TCGplayer market, CK retail, Cardmarket trend)
  - `b`: buylist
  - `low` e `avg`: estatísticas do Cardmarket
  - `nm`, `ex`, `vg` e `g`: preço por condição (CK)
- Cada fonte vem na própria moeda. O app converte usando `fx`.

## Fontes

| Fonte | Origem |
|---|---|
| Catálogo e imagens | [Scryfall](https://scryfall.com) bulk `default_cards`. Imagens servidas por `cards.scryfall.io`. |
| TCGplayer | [MTGJSON](https://mtgjson.com) `AllPricesToday` (MIT). O id do MTGJSON é ligado ao id do Scryfall via `cardIdentifiers.csv`. |
| Card Kingdom | `api.cardkingdom.com/api/v2/pricelist` (já vem com `scryfall_id`). Se faltar algum preço, usa o MTGJSON. |
| Cardmarket | `price_guide_1.json` (`idProduct` = `cardmarket_id` do Scryfall). Se faltar algum preço, usa o MTGJSON. |
| Câmbio | [Frankfurter](https://frankfurter.dev) (BCE). |

## Rodar local

```bash
python -I pipeline/build.py --work work --out out --cache
```

Usa só a biblioteca padrão do Python 3.10+. O `--cache` reaproveita os downloads que já estão em `work/`.
