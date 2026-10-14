# assets — screenshots for the README

Images linked from the **Web interface** section of
[`README.md`](../README.md). Nothing here is used by the program — the page is
plain HTML/CSS/JS — these files exist only so the README can show what `mpt web`
looks like.

Shoot them at a normal desktop width (about 1400×900 or wider) with the browser
chrome cropped out, PNG format.

| File | What it must show |
|---|---|
| `web-page.png` | The idle page. The `batch` stage open in the sidebar, its flags filled (**Fill from config** pressed), the pipeline map visible, and both header dots readable — the MPT dot should be *green* (`MPT API: up`). This is the "everything at rest" shot. |
| `web-run.png` | A run in flight. The log streaming line by line, the run dot showing `running` with the elapsed-time clock ticking, and the Cancel button visible. Stop it mid-output, not at the end. |

A missing file breaks the image link in the README but nothing else. Until both
exist, GitHub simply renders the alt text.
