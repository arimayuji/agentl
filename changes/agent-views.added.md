`views:` on agent steps (#214): derived views sent to the llm in place of their source field.
`pages` cuts a form-feed text or list to the selected pages, `pdf.text` and `pdf.tables` read
only the selected pages of a PDF, and `html.compact` strips scripts, styles and attributes.
Each view is cached by the SHA-256 of its source, PDF views run with pypdf's module settings
isolated, and the step's spend reports pages and characters sent.
