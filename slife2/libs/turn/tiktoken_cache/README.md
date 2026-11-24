# The bundled tokenizer vocabulary

`fb374d419588a4632f3f557e76b4b70aebbca790` is `tiktoken`'s cache file for

    https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken

The file name is the SHA-1 of that URL, which is the key `tiktoken.load.read_file_cached`
uses. `slife2.libs.turn.tokens` points `TIKTOKEN_CACHE_DIR` here before asking for the
`o200k_base` encoding, so the load is a local read and never touches the network.

- size: 3,613,922 bytes
- SHA-256: `446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d`
- source: the `o200k_base` entry in `tiktoken 0.14.0`'s `tiktoken_ext.openai_public`

To update it, fetch the URL, verify the content against the `expected_hash` in the
`tiktoken` release that names it, and replace this file under the URL's SHA-1 name.
`tests/libs/turn/test_tokens.py` proves the load makes no network request, so a
stale name fails there rather than at a user's first recall.
