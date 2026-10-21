`chunk: {by: page, size: N}` on agent steps with `output_mode: rows` (#215): each input record
is split into groups of `size` pages (the items of `field`, default `text`, when it is a list,
or its text split on form feeds) and asked once per group, so a long document does not hit the
output limit in one answer. Rejected rows carry `_pages`.
