# NF-e and NFC-e invoices

`nfe.read` turns Brazilian electronic invoice XML into records: NF-e (model 55) and NFC-e
(model 65), authorised documents (`nfeProc`, with the protocol) and bare `NFe` documents. It reads
paths, globs, remote storage, URLs and zip files, with no extra dependency and no LLM.

```yaml
- id: notes
  use: nfe.read
  with: { path: "xml/*.xml", level: item }   # or level: note (default)
```

## Fields

`level: note` gives one record per invoice:

- `chave` (44 digits), `number`, `series`, `model`, `issued_at`
- `issuer_doc`, `issuer_name`, `issuer_state`, `recipient_doc`, `recipient_name`, `recipient_state`
- `total_products`, `total_icms`, `total_ipi`, `total_pis`, `total_cofins`, `total_freight`,
  `total_discount`, `total`
- `protocol_status`, `protocol_number`, `protocol_at`
- `cancelled` (and `cancelled_at`), `_file`

`level: item` gives one record per item with the note fields too, plus `item`, `code`,
`description`, `ncm`, `cfop`, `unit`, `quantity`, `unit_value`, `item_total`, and the per-item
taxes `icms_base`, `icms`, `ipi_base`, `ipi`, `pis_base`, `pis`, `cofins_base`, `cofins`.

Money and quantities are numbers, dates are the ISO text from the XML, and a CNPJ or CPF keeps its
digits only.

## Cancellations and validation

A cancellation event file (`procEventoNFe`, event `110111` or `110112`) in the same batch marks its
note `cancelled: true`; the note is kept.

A file that cannot be parsed, or is not an NF-e document, goes to the step's rejected records with
`_error` and `_file`; the run goes on. With `validate: true` the reader also rejects notes whose
`chave` check digit is wrong and events whose note is not among the files.

## Not here

No SEFAZ access (downloads or status queries), no signing or issuing of invoices.

## From a folder to a spreadsheet

The `nfe-to-excel` template reads `xml/*.xml` at item level, has an optional agent step that picks
an expense category per item, and writes `out/nfe-items.xlsx`:

```bash
agentl new --template nfe-to-excel -o pipeline.yaml   # then put the XML files in xml/
```

See the [recipe](recipes.md#nf-e-to-excel) for the full pipeline.

## Speed

On 10,000 synthetic files (3 items each), `nfe.read` to CSV takes about 3.5x a hand-written
`xml.etree` script; see [Performance](performance.md#nf-e-xml-against-xmletree).
