`pdf.read` joins the text of its pages with a form feed (`\f`) instead of a newline, so
`chunk: {by: page}` can split it again.
