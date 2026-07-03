#!/usr/bin/env python3
"""
refill.py — shorts-pilot entry point.

Checks how many jobs are pending in jobs_<lang>.yaml and refills
the queue by generating new video ideas via LLM when it runs low.

Where jobs/seen files live (priority order):
    1. --jobs-dir / --seen-dir (explicit CLI flags)
    2. paths.jobs_dir / paths.seen_dir in config.yaml
    3. current directory

Usage:
    refill --lang en                                   # uses config.yaml paths, or cwd
    refill --lang en --jobs-dir /your/path/to/jobs
    refill --lang es --jobs-dir /your/path/to/jobs
    refill --lang en --jobs-dir /your/path/to/jobs --force
    refill --lang en --jobs-dir /your/path/to/jobs --count 50
    refill --lang en --jobs-dir /your/path/to/jobs --threshold 5
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from shorts_pilot.generator import jobs, seen
from shorts_pilot.generator.seen import load_ordered as seen_load_ordered
from shorts_pilot.generator.llm import call_llm, parse_json_array
from shorts_pilot.generator.prompt import VIDEO_SUBJECT_MAX_CHARS, build as build_prompt
from shorts_pilot.generator.settings import LangSettings, load as load_settings

# ── Deduplication + cleanup ───────────────────────────────────────────────────

def _validate_against_config(job: dict, lang_cfg: LangSettings) -> dict:
    """
    Check the fields the LLM was asked to fill in (voice_name, voice_rate,
    video_clip_duration, bgm_volume, paragraph_number, video_concat_mode,
    bgm_type) against config.yaml. Anything missing, the wrong type, or out
    of range is replaced with a safe configured default instead of being
    written into jobs_<lang>.yaml as-is.
    """
    defaults = lang_cfg.job_defaults
    out = dict(job)

    voices = lang_cfg.voices
    if voices and out.get("voice_name") not in voices:
        out["voice_name"] = voices[0]

    rate = out.get("voice_rate")
    if not isinstance(rate, (int, float)) or isinstance(rate, bool) or not (
        lang_cfg.voice_rate_min <= rate <= lang_cfg.voice_rate_max
    ):
        out["voice_rate"] = lang_cfg.voice_rate_min

    clip = out.get("video_clip_duration")
    if not isinstance(clip, (int, float)) or isinstance(clip, bool) or clip <= 0:
        out["video_clip_duration"] = defaults.get("video_clip_duration", 3)

    volume = out.get("bgm_volume")
    if not isinstance(volume, (int, float)) or isinstance(volume, bool) or not (0 <= volume <= 1):
        out["bgm_volume"] = defaults.get("bgm_volume", 0.15)

    para = out.get("paragraph_number")
    if not isinstance(para, int) or isinstance(para, bool) or para <= 0:
        out["paragraph_number"] = defaults.get("paragraph_number", 2)

    if not isinstance(out.get("video_concat_mode"), str) or not out.get("video_concat_mode"):
        out["video_concat_mode"] = defaults.get("video_concat_mode", "random")

    if not isinstance(out.get("bgm_type"), str) or not out.get("bgm_type"):
        out["bgm_type"] = defaults.get("bgm_type", "random")

    return out


# A subject this short can't fill a 30-45s video — treat it the same as a
# missing one (see fix #6: previously "" silently passed straight through).
_MIN_VIDEO_SUBJECT_CHARS = 30


def _truncate_subject(subject: str, max_chars: int) -> str:
    """
    Truncate `subject` to at most `max_chars`, preferring a sentence
    boundary, then a word boundary, over a hard mid-word/mid-sentence cut
    (see fix #11: a plain subject[:max_chars] slice could previously leave
    a narration ending mid-word).
    """
    if len(subject) <= max_chars:
        return subject

    window = subject[:max_chars]

    # Prefer the last sentence-ending punctuation in the window, as long
    # as it isn't so early we'd throw away most of the allowed content.
    last_sentence_end = max(window.rfind("."), window.rfind("!"), window.rfind("?"))
    if last_sentence_end >= max_chars * 0.5:
        return window[:last_sentence_end + 1]

    # No good sentence boundary — fall back to the last word boundary so
    # we at least don't cut a word in half, and round it off with a period.
    last_space = window.rfind(" ")
    if last_space >= max_chars * 0.5:
        truncated = window[:last_space].rstrip()
        if truncated and truncated[-1] not in ".!?":
            truncated += "."
        return truncated

    # No usable boundary at all (e.g. one very long token) — hard cut.
    return window


def _normalise(
        job: dict,
        expected_suffix: str,
        lang_cfg: LangSettings | None = None,
        foreign_suffixes: set[str] | None = None,
) -> dict:
    """
    Return a cleaned copy of job with:
    - enabled forced to True
    - path separators stripped from output_file
    - output_file lowercased, so dedup/seen-tracking can't be evaded by a
      case variant of an already-used name, and so two "different" queued
      filenames can't collide on a case-insensitive filesystem
    - missing file_suffix corrected on output_file. For the default
      (empty-suffix) language specifically, a *foreign* suffix accidentally
      present (e.g. "..._es.mp4" while generating for "en") is stripped
      instead — previously nothing validated this case at all, so such a
      file could be permanently misclassified by init-seen's suffix filter
    - video_subject clamped to VIDEO_SUBJECT_MAX_CHARS
    - voice/timing/bgm fields validated against lang_cfg (when provided),
      falling back to configured defaults instead of raising on bad LLM output

    Raises ValueError if video_subject is missing, empty, or too short to
    be a usable script — callers should treat that as a malformed job (skip,
    don't write), the same as a missing output_file.
    """
    output_file = job.get("output_file", "") or ""
    if not isinstance(output_file, str):
        output_file = str(output_file)

    # Strip any accidental path components (e.g. "subdir/fact.mp4" → "fact.mp4"),
    # then lowercase — see docstring above.
    output_file = Path(output_file).name.lower()

    stem = output_file.removesuffix(".mp4")
    if expected_suffix:
        # Ensure this lang's own suffix is present.
        suffix_lower = expected_suffix.lower()
        if not stem.endswith(suffix_lower):
            stem = f"{stem}{suffix_lower}"
    elif foreign_suffixes:
        for fs in foreign_suffixes:
            fs = (fs or "").lower()
            if fs and stem.endswith(fs) and len(stem) > len(fs):
                stem = stem[: len(stem) - len(fs)]
                break
    output_file = f"{stem}.mp4"

    subject = job.get("video_subject", "") or ""
    if not isinstance(subject, str):
        subject = str(subject)
    subject = subject.strip()
    if len(subject) < _MIN_VIDEO_SUBJECT_CHARS:
        raise ValueError(
            f"video_subject is missing or too short ({len(subject)} chars, "
            f"need at least {_MIN_VIDEO_SUBJECT_CHARS})"
        )
    if len(subject) > VIDEO_SUBJECT_MAX_CHARS:
        subject = _truncate_subject(subject, VIDEO_SUBJECT_MAX_CHARS)

    clean = {
        **job,
        "output_file": output_file,
        "enabled": True,
        "video_subject": subject,
    }

    if lang_cfg is not None:
        clean = _validate_against_config(clean, lang_cfg)

    return clean


def _deduplicate(
        raw_jobs: list[dict],
        already_known: set[str],
        lang_cfg: LangSettings | None = None,
        expected_suffix: str = "",
        foreign_suffixes: set[str] | None = None,
) -> list[dict]:
    # Lowercase for comparison: new output_file values are always already
    # lowercased by _normalise, but already_known (seen.txt + existing yaml
    # entries) may still hold case-varied names predating this fix, or from
    # a manual edit — comparing case-insensitively catches those too.
    known = {n.lower() for n in already_known}
    known_names: set[str] = set()
    result = []
    for job in raw_jobs:
        if not isinstance(job, dict):
            print(f"  [skip] malformed job (not an object): {job!r}")
            continue

        output_file = job.get("output_file", "")
        if not output_file:
            print(f"  [skip] job missing output_file: {job.get('name', '?')}")
            continue

        try:
            # Normalise first so we dedup against the corrected filename.
            # A single malformed job (e.g. a null field where a string was
            # expected, or a missing/empty video_subject) must not take the
            # rest of the batch down with it.
            clean = _normalise(job, expected_suffix, lang_cfg, foreign_suffixes)
        except Exception as e:
            print(f"  [skip] malformed job {job.get('name', '?')!r}: {e}")
            continue

        output_file = clean["output_file"]  # already lowercased by _normalise

        if output_file in known:
            print(f"  [skip duplicate] {output_file}")
            continue

        if not clean.get("name") or not isinstance(clean.get("name"), str):
            clean = {**clean, "name": jobs.safe_name(output_file.replace(".mp4", ""))}

        # Keep job names unique too — the LLM only dedups output_file
        # against ALREADY USED TOPICS, so within a single batch it can
        # still produce two jobs that reduce to the same name.
        name = clean["name"]
        if name in known_names:
            base, i = name, 2
            while f"{base}_{i}" in known_names:
                i += 1
            name = f"{base}_{i}"
            clean = {**clean, "name": name}
        known_names.add(name)

        known.add(output_file)
        result.append(clean)
    return result


# ── Core logic ────────────────────────────────────────────────────────────────

# If dedup leaves the queue still under threshold after a batch, try again
# a bounded number of extra times rather than silently stopping short — but
# give up early once an attempt yields nothing new, so a persistently
# unproductive LLM/prompt combination can't burn unbounded API calls.
_MAX_TOPUP_ATTEMPTS = 2


def run(
        lang: str,
        jobs_dir: Path | None,
        seen_dir: Path | None,
        force: bool,
        count_override: int | None,
        threshold_override: int | None,
) -> int:
    settings = load_settings()

    # Priority: explicit CLI flag > paths.* from config.yaml > current directory.
    jobs_dir = (jobs_dir or settings.jobs_dir or Path(".")).resolve()
    seen_dir = (seen_dir or settings.seen_dir or jobs_dir).resolve()

    if not jobs_dir.is_dir():
        raise FileNotFoundError(f"jobs directory not found: {jobs_dir}")

    lang_cfg = settings.lang(lang)
    suffix = lang_cfg.file_suffix

    seen_set = seen.load(seen_dir, suffix)
    seen_list = seen_load_ordered(seen_dir, suffix)

    threshold = threshold_override if threshold_override is not None else settings.refill_threshold
    generate_count = count_override if count_override is not None else settings.generate_count

    # Load YAML once; derive both pending count and existing names from it.
    cfg = jobs.load(jobs_dir, lang)
    pending = jobs.count_pending_from(cfg, seen_set)

    seen_file = "seen.txt" if not suffix else f"seen_{suffix.lstrip('_')}.txt"
    print(f"[{lang}] jobs dir: {jobs_dir}")
    print(f"[{lang}] pending jobs: {pending} | threshold: {threshold} | seen file: {seen_file} (in {seen_dir})")

    if pending >= threshold and not force:
        print(f"[{lang}] Queue is full — nothing to do. (Use --force to override.)")
        return 0

    existing = jobs.existing_names_from(cfg)
    already_known = seen_set | existing
    print(f"[{lang}] known titles: {len(already_known)} | model: {settings.model}")

    # Other configured languages' suffixes, used so an output_file that
    # accidentally ends in e.g. "_es" while generating for a different
    # (or the default, empty-suffix) language gets caught — see fix #7
    # in _normalise().
    foreign_suffixes = {
        c.file_suffix for code, c in settings.langs.items()
        if code != lang and c.file_suffix
    }

    # seen_list only covers rendered videos (seen.txt). Topics already
    # queued in jobs_<lang>.yaml but not yet rendered are just as much
    # "already used" from the LLM's point of view — without them the
    # prompt's dedup context misses the whole pending backlog, and the
    # LLM can propose near-duplicates of ideas that simply haven't been
    # rendered yet. Append pending-only names (not already in seen_list)
    # after seen_list so they're prioritised by the prompt's recency
    # slice (build_prompt keeps the *last* N entries). This list is grown
    # below with each top-up attempt's own new names too.
    existing_ordered = jobs.existing_names_ordered_from(cfg)
    pending_ordered = [n for n in existing_ordered if n not in seen_set]
    prompt_seen_ordered = seen_list + pending_ordered

    total_added = 0
    attempt = 0

    while True:
        attempt += 1
        is_topup = attempt > 1

        # The first call always uses the configured/overridden count as-is.
        # A top-up call asks for at least that many again, or however many
        # are still needed to cross the threshold if that's more.
        if is_topup:
            remaining_needed = max(threshold - (pending + total_added), 1)
            this_count = max(generate_count, remaining_needed)
            print(f"[{lang}] still under threshold ({pending + total_added}/{threshold}) "
                  f"— topping up (attempt {attempt - 1}/{_MAX_TOPUP_ATTEMPTS})...")
        else:
            this_count = generate_count

        system_prompt, user_prompt = build_prompt(
            lang_cfg, already_known, this_count, seen_ordered=prompt_seen_ordered
        )
        print(f"[{lang}] calling LLM for {this_count} ideas...")

        raw_text = call_llm(system_prompt, user_prompt, settings, this_count)
        raw_jobs = parse_json_array(raw_text)

        if len(raw_jobs) < this_count:
            print(f"[{lang}] WARNING: LLM returned {len(raw_jobs)} of {this_count} requested — queue may still be low after this run")

        print(f"[{lang}] LLM returned {len(raw_jobs)} raw jobs")

        clean_jobs = _deduplicate(
            raw_jobs, already_known, lang_cfg,
            expected_suffix=suffix, foreign_suffixes=foreign_suffixes,
        )
        print(f"[{lang}] after dedup: {len(clean_jobs)} new jobs")

        if clean_jobs:
            jobs.append(jobs_dir, lang, clean_jobs)
            print(f"[{lang}] appended {len(clean_jobs)} jobs to jobs_{lang}.yaml")
            total_added += len(clean_jobs)

            # Feed this attempt's new names into the next attempt's dedup
            # context, so a top-up round doesn't just re-propose what the
            # previous round already added.
            new_names = [j["output_file"] for j in clean_jobs]
            already_known |= set(new_names)
            prompt_seen_ordered += new_names

        if pending + total_added >= threshold:
            break
        if not clean_jobs:
            if is_topup:
                print(f"[{lang}] top-up attempt produced nothing new — stopping")
            break
        if attempt > _MAX_TOPUP_ATTEMPTS:
            break

    if total_added == 0:
        print(f"[{lang}] nothing new after dedup — try again or use --force")
        return 0

    # Note: seen.txt is updated by batch_generate.py after each video is rendered,
    # not here — refill only writes to the jobs yaml.
    return total_added


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="refill",
        description="Auto-refill your MoneyPrinterTurbo jobs queue with LLM-generated video ideas.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    refill --lang en
    refill --lang es
    refill --lang en --jobs-dir /your/path/to/jobs
    refill --lang es --jobs-dir /your/path/to/jobs
    refill --lang en --jobs-dir /your/path/to/jobs --force
    refill --lang en --jobs-dir /your/path/to/jobs --count 50
    refill --lang en --jobs-dir /your/path/to/jobs --threshold 5
""",
    )
    parser.add_argument("--lang", required=True, metavar="LANG",
                        help="Language code (e.g. en, es). Must be defined in config.yaml.")
    parser.add_argument("--jobs-dir", type=Path, default=None, metavar="PATH",
                        help="Directory containing jobs_<lang>.yaml files. "
                             "Default: paths.jobs_dir from config.yaml, else current directory.")
    parser.add_argument("--seen-dir", type=Path, default=None, metavar="PATH",
                        help="Directory for seen_<lang>.txt files. "
                             "Default: paths.seen_dir from config.yaml, else --jobs-dir.")
    parser.add_argument("--force", action="store_true",
                        help="Refill even if the queue is above the threshold.")
    parser.add_argument("--count", type=int, default=None, metavar="N",
                        help="Override generation.count from config.yaml.")
    parser.add_argument("--threshold", type=int, default=None, metavar="N",
                        help="Override generation.threshold from config.yaml.")

    args = parser.parse_args()

    if args.count is not None and args.count <= 0:
        print("[ERROR] --count must be a positive integer")
        sys.exit(1)
    if args.threshold is not None and args.threshold < 0:
        print("[ERROR] --threshold must be a non-negative integer")
        sys.exit(1)

    jobs_dir = args.jobs_dir.resolve() if args.jobs_dir else None
    seen_dir = args.seen_dir.resolve() if args.seen_dir else None

    try:
        added = run(
            lang=args.lang,
            jobs_dir=jobs_dir,
            seen_dir=seen_dir,
            force=args.force,
            count_override=args.count,
            threshold_override=args.threshold,
        )
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

    print(f"\n[done] added {added} new jobs.")


if __name__ == "__main__":
    main()