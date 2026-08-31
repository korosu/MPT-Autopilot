"""
One-off import tool (Etap 1): rewrite each source repository's history so that
every file already lives at its final monorepo path, then fast-import it into
the monorepo as refs/heads/import/<name>.

Rewriting paths *before* the merge (instead of read-tree --prefix afterwards)
is what makes `git log <path>` and `git log --follow <path>` traverse into the
original history: the imported commits touch the final paths directly, so no
rename has to be detected across a merge boundary.

Mappings cover historical layouts too (shorts-pilot/hashtag-enricher were
flat before their src/ reorganisation), so --follow chains through those
reorganisations as well.

Usage:  python import_history.py <source-repo> <mapping-name> <dest-repo> <ref>
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# ── Path mappings ─────────────────────────────────────────────────────────────
# Each mapping is {historical path: final path}. A path absent from the mapping
# is dropped from the imported history (per-project LICENSE, pyproject.toml,
# uv.lock, .gitignore, .github/, requirements.txt, per-project config examples,
# __init__.py files, and the three duplicate notify.py copies).

MAPPINGS: dict[str, dict[str, str]] = {
    "mpt-batch": {
        "src/mpt_batch/batch.py": "src/mpt_autopilot/batch/run.py",
        "src/mpt_batch/engine/api.py": "src/mpt_autopilot/batch/api.py",
        "src/mpt_batch/engine/bgm.py": "src/mpt_autopilot/batch/bgm.py",
        "src/mpt_batch/engine/settings.py": "src/mpt_autopilot/batch/settings.py",
        "src/mpt_batch/engine/state.py": "src/mpt_autopilot/batch/state.py",
        "src/mpt_batch/engine/voices.py": "src/mpt_autopilot/batch/voices.py",
        "src/mpt_batch/data/edge_voices.json": "src/mpt_autopilot/batch/data/edge_voices.json",
        # notify.py: this is the copy that survives as the single shared notifier
        "src/mpt_batch/engine/notify.py": "src/mpt_autopilot/notify.py",
        "tests/test_api_state.py": "tests/batch/test_api_state.py",
        "tests/test_bgm.py": "tests/batch/test_bgm.py",
        "tests/test_lang.py": "tests/batch/test_lang.py",
        "tests/test_seen.py": "tests/batch/test_seen.py",
        "tests/test_settings.py": "tests/batch/test_settings.py",
        "tests/test_state.py": "tests/batch/test_state.py",
        "tests/test_voices.py": "tests/batch/test_voices.py",
        "tests/test_notify.py": "tests/common/test_notify.py",
        "README.md": "docs/batch.md",
        "jobs.example.yaml": "jobs.example.yaml",
    },
    "shorts-pilot": {
        # flat layout (pre-src/ reorganisation)
        "refill.py": "src/mpt_autopilot/pilot/refill.py",
        "init_seen.py": "src/mpt_autopilot/pilot/init_seen.py",
        "generator/jobs.py": "src/mpt_autopilot/pilot/jobs.py",
        "generator/llm.py": "src/mpt_autopilot/pilot/llm.py",
        "generator/prompt.py": "src/mpt_autopilot/pilot/prompt.py",
        "generator/settings.py": "src/mpt_autopilot/pilot/settings.py",
        "generator/seen.py": "src/mpt_autopilot/seen.py",
        # src/ layout
        "src/shorts_pilot/refill.py": "src/mpt_autopilot/pilot/refill.py",
        "src/shorts_pilot/init_seen.py": "src/mpt_autopilot/pilot/init_seen.py",
        "src/shorts_pilot/generator/jobs.py": "src/mpt_autopilot/pilot/jobs.py",
        "src/shorts_pilot/generator/llm.py": "src/mpt_autopilot/pilot/llm.py",
        "src/shorts_pilot/generator/prompt.py": "src/mpt_autopilot/pilot/prompt.py",
        "src/shorts_pilot/generator/settings.py": "src/mpt_autopilot/pilot/settings.py",
        # seen.py: this is the copy that survives as the single shared registry
        "src/shorts_pilot/generator/seen.py": "src/mpt_autopilot/seen.py",
        "src/shorts_pilot/generator/lock.py": "src/mpt_autopilot/lock.py",
        "tests/test_count.py": "tests/pilot/test_count.py",
        "tests/test_duration.py": "tests/pilot/test_duration.py",
        "tests/test_reasoning.py": "tests/pilot/test_reasoning.py",
        "README.md": "docs/pilot.md",
    },
    "hashtag-enricher": {
        # flat layout (pre-src/ reorganisation)
        "enrich.py": "src/mpt_autopilot/enricher/run.py",
        "enricher/config.py": "src/mpt_autopilot/enricher/settings.py",
        "enricher/llm.py": "src/mpt_autopilot/enricher/llm.py",
        "enricher/logger.py": "src/mpt_autopilot/logging.py",
        "enricher/reader.py": "src/mpt_autopilot/enricher/reader.py",
        "enricher/writer.py": "src/mpt_autopilot/enricher/writer.py",
        # src/ layout
        "src/hashtag_enricher/enrich.py": "src/mpt_autopilot/enricher/run.py",
        "src/hashtag_enricher/enricher/config.py": "src/mpt_autopilot/enricher/settings.py",
        "src/hashtag_enricher/enricher/llm.py": "src/mpt_autopilot/enricher/llm.py",
        "src/hashtag_enricher/enricher/logger.py": "src/mpt_autopilot/logging.py",
        "src/hashtag_enricher/enricher/postprocess.py": "src/mpt_autopilot/enricher/postprocess.py",
        "src/hashtag_enricher/enricher/reader.py": "src/mpt_autopilot/enricher/reader.py",
        "src/hashtag_enricher/enricher/writer.py": "src/mpt_autopilot/enricher/writer.py",
        "tests/test_writer.py": "tests/enricher/test_writer.py",
        "README.md": "docs/enricher.md",
    },
    "yt-shorts-uploader": {
        "src/yt_uploader/upload.py": "src/mpt_autopilot/uploader/run.py",
        "src/yt_uploader/engine/settings.py": "src/mpt_autopilot/uploader/settings.py",
        "src/yt_uploader/engine/uploader.py": "src/mpt_autopilot/uploader/uploader.py",
        "src/yt_uploader/engine/metadata.py": "src/mpt_autopilot/uploader/metadata.py",
        "src/yt_uploader/engine/ledger.py": "src/mpt_autopilot/uploader/ledger.py",
        "tests/test_ledger.py": "tests/uploader/test_ledger.py",
        "tests/test_metadata.py": "tests/uploader/test_metadata.py",
        "tests/test_settings.py": "tests/uploader/test_settings.py",
        "tests/test_upload_main.py": "tests/uploader/test_upload_main.py",
        "tests/test_upload_run.py": "tests/uploader/test_upload_run.py",
        "tests/test_uploader.py": "tests/uploader/test_uploader.py",
        "README.md": "docs/uploader.md",
        "accounts.example.yaml": "accounts.example.yaml",
    },
}


class StreamRewriter:
    """Rewrites a `git fast-export` stream, remapping or dropping file paths."""

    def __init__(self, data: bytes, mapping: dict[str, str]) -> None:
        self.data = data
        self.pos = 0
        self.mapping = {k.encode(): v.encode() for k, v in mapping.items()}
        self.out = bytearray()

    def _readline(self) -> bytes | None:
        if self.pos >= len(self.data):
            return None
        end = self.data.find(b"\n", self.pos)
        if end == -1:
            line, self.pos = self.data[self.pos :], len(self.data)
            return line
        line = self.data[self.pos : end + 1]
        self.pos = end + 1
        return line

    def _read_exact(self, count: int) -> bytes:
        chunk = self.data[self.pos : self.pos + count]
        self.pos += count
        return chunk

    def _emit_data_block(self, header: bytes) -> None:
        """Copy a `data <len>` block verbatim (binary-safe)."""
        self.out += header
        length = int(header.split()[1])
        self.out += self._read_exact(length)

    def _map(self, path: bytes) -> bytes | None:
        return self.mapping.get(path.strip(b'"'))

    def run(self) -> bytes:
        while True:
            line = self._readline()
            if line is None:
                break

            if line.startswith(b"data "):
                self._emit_data_block(line)
                continue

            if line.startswith(b"M "):
                # M <mode> <dataref> <path>
                parts = line.rstrip(b"\n").split(b" ", 3)
                target = self._map(parts[3])
                if target is not None:
                    self.out += b" ".join(parts[:3]) + b" " + target + b"\n"
                continue

            if line.startswith(b"D "):
                target = self._map(line.rstrip(b"\n")[2:])
                if target is not None:
                    self.out += b"D " + target + b"\n"
                continue

            # Tags would point at objects we may have filtered out.
            if line.startswith(b"tag "):
                # skip the whole tag record: from / tagger / data
                while True:
                    nxt = self._readline()
                    if nxt is None:
                        return bytes(self.out)
                    if nxt.startswith(b"data "):
                        length = int(nxt.split()[1])
                        self._read_exact(length)
                        break
                continue

            self.out += line

        return bytes(self.out)


def main() -> int:
    if len(sys.argv) != 5:
        print(__doc__, file=sys.stderr)
        return 2

    source, mapping_name, dest, ref = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    mapping = MAPPINGS.get(mapping_name)
    if mapping is None:
        print(f"unknown mapping '{mapping_name}'", file=sys.stderr)
        return 2

    exported = subprocess.run(
        [
            "git",
            "-C",
            source,
            "fast-export",
            "--reencode=yes",
            "--signed-tags=strip",
            "--tag-of-filtered-object=drop",
            "--refspec",
            f"refs/heads/main:{ref}",
            "refs/heads/main",
        ],
        capture_output=True,
        check=True,
    )

    rewritten = StreamRewriter(exported.stdout, mapping).run()

    imported = subprocess.run(
        ["git", "-C", dest, "fast-import", "--force", "--quiet"],
        input=rewritten,
        capture_output=True,
    )
    if imported.returncode != 0:
        sys.stderr.write(imported.stderr.decode("utf-8", "replace"))
        return imported.returncode

    print(f"imported {mapping_name} -> {ref} ({len(rewritten)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
