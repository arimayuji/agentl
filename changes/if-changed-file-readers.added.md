`if_changed` and `on_unchanged` on the file readers that take a `url` (`file.read`,
`archive.read`, `csv.read`, `json.read`, `txt.read`, `excel.read`, `parquet.read`) (#279): the
same ETag / Last-Modified / Content-Length / page stamp / hash check as `http.get`, so an
unchanged file ends the run as `no_change` (or gives no records) instead of being downloaded
and read again.
