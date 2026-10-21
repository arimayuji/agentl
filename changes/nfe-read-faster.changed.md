`nfe.read` is about 1.8x faster and holds less memory on large folders: one element lookup instead
of two, and each parsed file is freed once its items are read (#161).
