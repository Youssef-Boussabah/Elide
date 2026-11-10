# Commit SHA map (privacy rewrite, 2026-11-10)

Before first publication, the author/committer email of all local commits was changed
from the owner's personal address to the GitHub noreply address (owner decision).
Only commit metadata changed: every commit's tree hash is identical, so file contents
are byte-for-byte the same. Evidence files and the tracker keep the SHAs that were
current when they were written; use this table to find the published commit.

| Original SHA (cited in evidence) | Published SHA | Tree (unchanged) | Subject |
|---|---|---|---|
| `6295cef` (`6295cefb9152f6d49e1bbc06524bed084c2bfabd`) | `70e2f62` | `0949b17825` | P0: record owner decisions F1/F2, settlement record, audit fixes |
| `a8fef3c` (`a8fef3c241f35e3ae6b12c7a35cffbf55661116c`) | `a875862` | `27e125b78d` | P0: verify Sandbox payment of INV2-4RUV-XPEH-4F3C-T8BW via provider GET |
| `265726e` (`265726e1cd628326bd15f2335f33d83766aac0a8`) | `d34ad21` | `e5120dc329` | P0: enforce single SUCCESS transaction on PAID; read-only capture lookup |
| `1316f79` (`1316f79ce671702b59ca60e1e7ef1797b9d2d64c`) | `6a627b7` | `193e17df42` | P0: add browser automation policy to agent instructions |
| `b83e107` (`b83e10743b395c433359247b20248ec3344212a9`) | `bb06db3` | `229f569e03` | P0: Sandbox invoice due date set and sent; awaiting payer payment |
| `c80173d` (`c80173d169f3b30af1fef52bf31b42e172dce4fd`) | `9d9ad26` | `94f2aa7ff2` | P0: add due-date update and send-by-id modes to Sandbox check |
| `efef77c` (`efef77c2ffa1f5f506a871e7f468dda0a2be4b2e`) | `793b34c` | `e99d0075a9` | P0: Sandbox-verify OAuth, Invoicing access and draft invoice |
| `64af80c` (`64af80c252e499a8ef2b75d046a93ff49b4fa143`) | `14fe6d2` | `c4544104e8` | P0: record bootstrap commit SHA in phase tracker |
| `16c7276` (`16c7276844c59003e9e6345d3dfa836daa65b8d9`) | `0b4a303` | `54ee8ec5f5` | P0: bootstrap repository |
