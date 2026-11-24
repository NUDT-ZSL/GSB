# reports

Generated JSON reports are written here by default. Files are first written to a
same-directory temporary file, fsynced, and then atomically replaced. Generated
reports are ignored by Git.
