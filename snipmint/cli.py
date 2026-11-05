"""Command line interface (NarraReel NarraReel WHAT_TO_DO.md section 5.16)."""

from __future__ import annotations

import functools
import json
import logging
import platform
import shutil
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from . import __version__, config
from .errors import ScriptError, SnipmintError
from .models import ProgressEvent, VoiceSelection

app = typer.Typer(
    name="snipmint",
    help="Turn your script into narration, timings and captions. Your script. Your voice.",
    no_args_is_help=True,
    add_completion=False,
)

DEFAULT_VOICE = "edge:en-US-AvaNeural"


def _version(value: bool) -> None:
    if value:
        typer.echo(f"snipmint {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    verbose: Annotated[int, typer.Option("--verbose", "-v", count=True, help="-v info, -vv debug")] = 0,
    version: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="Show version")
    ] = False,
) -> None:
    level = {0: logging.WARNING, 1: logging.INFO}.get(verbose, logging.DEBUG)
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")


def friendly_errors(fn):
    """Print Snipmint errors as one clear message instead of a traceback."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except SnipmintError as exc:
            typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from None

    return wrapper


def read_script(path: str) -> str:
    if path == "-":
        text = sys.stdin.read()
    else:
        p = Path(path)
        if not p.is_file():
            raise ScriptError(f"Script file not found: {path}")
        text = p.read_text(encoding="utf-8-sig")
    if not text.strip():
        raise ScriptError("The script is empty. Add some text first.")
    return text


def _emit_json(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _progress_printer(enabled: bool):
    state = {"last": ""}

    def show(event: ProgressEvent) -> None:
        if not enabled:
            return
        line = f"[{int(event.fraction * 100):3d}%] {event.message}"
        if line != state["last"]:
            typer.echo(line, err=True)
            state["last"] = line

    return show


def _download_progress(enabled: bool):
    state = {"pct": -1}

    def show(fraction: float, message: str) -> None:
        pct = int(fraction * 100)
        if enabled and pct != state["pct"] and pct % 5 == 0:
            typer.echo(f"  {message}: {pct}%", err=True)
        state["pct"] = pct

    return show


# ------------------------------------------------------------------ doctor


@app.command()
@friendly_errors
def doctor(json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False):
    """Show the Snipmint version and check Python, ffmpeg, voice engines and disk space."""
    from .audio import ffmpeg as ff
    from .engines import registry

    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool | None, detail: str, core: bool = True) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "core": core})

    add("Snipmint", True, __version__)

    py = sys.version_info
    add("Python", py >= (3, 10), f"{platform.python_version()} ({sys.executable})")

    try:
        add("ffmpeg", True, f"{ff.find_ffmpeg()} (version {ff.version()})")
        add("ffprobe", True, ff.find_ffprobe())
        libass = ff.has_libass()
        add(
            "libass (captions)",
            libass,
            "available" if libass else "missing - captions need an ffmpeg build with libass",
        )
    except SnipmintError as exc:
        add("ffmpeg", False, str(exc))

    for info in registry.available_engines():
        s = info.status
        if s.available and not s.needs_download:
            add(info.display_name, True, "ready", core=info.id == "edge")
        elif s.available:
            add(info.display_name, None, f"{s.reason} (~{s.download_size_mb} MB)", core=False)
        else:
            add(info.display_name, None, s.reason or "unavailable", core=False)

    try:
        free = shutil.disk_usage(config.home_dir().anchor or ".").free / 1e9
        add("Disk space", free > 2, f"{free:.1f} GB free")
    except OSError:
        pass
    add("Data folder", True, str(config.home_dir()))
    add("GPU", None, "NVIDIA GPU found" if shutil.which("nvidia-smi") else "none detected (CPU only)",
        core=False)  # fmt: skip

    if json_out:
        _emit_json(checks)
    else:
        for c in checks:
            mark = {True: "OK  ", False: "FAIL", None: "info"}[c["ok"]]
            color = {True: typer.colors.GREEN, False: typer.colors.RED, None: typer.colors.YELLOW}[c["ok"]]
            typer.echo(f"[{typer.style(mark, fg=color)}] {c['name']}: {c['detail']}")
    if any(c["ok"] is False and c["core"] for c in checks):
        raise typer.Exit(1)


# ------------------------------------------------------------------ voices


@app.command()
@friendly_errors
def voices(
    engine: Annotated[str | None, typer.Option(help="edge, kokoro, ...")] = None,
    lang: Annotated[str | None, typer.Option(help="Language prefix, e.g. en, en-US, hi")] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
):
    """List voices as <engine>:<voice> references you can pass to --voice."""
    from .engines import registry

    found = registry.list_voices(engine, lang)
    if json_out:
        _emit_json([v.model_dump() | {"ref": v.ref} for v in found])
        return
    if not found:
        typer.echo("No voices found.")
        return
    typer.echo(f"{'VOICE':38} {'NAME':22} {'LANG':8} {'GENDER':8} {'ACCENT':6} OFFLINE")
    for v in found:
        typer.echo(
            f"{v.ref:38} {v.name[:22]:22} {v.language:8} {v.gender:8} {(v.accent or '-'):6} "
            f"{'yes' if v.offline else 'no'}"
        )


# ----------------------------------------------------------------- analyze


@app.command()
@friendly_errors
def analyze(
    script: Annotated[str, typer.Argument(help="Script file ('-' reads stdin)")],
    voice: Annotated[str, typer.Option(help="Voice reference for the estimate")] = DEFAULT_VOICE,
    target: Annotated[str | None, typer.Option(help="Target length: 90s, 6m, 1:30 ...")] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
):
    """Count words, estimate narration length and plan how to hit a target."""
    from .script import analyze_script, format_duration, parse_duration, plan
    from .script.duration import get_cps

    analysis = analyze_script(read_script(script))
    cps, basis = get_cps(voice, analysis.language_hint)
    target_s = parse_duration(target) if target else None
    the_plan = plan(analysis, cps, target_s, basis)
    if json_out:
        _emit_json(
            {
                "words": analysis.word_count,
                "sentences": len(analysis.sentences),
                "paragraphs": analysis.paragraph_count,
                "language_hint": analysis.language_hint,
                "warnings": [w.model_dump() for w in analysis.warnings],
                "plan": the_plan.model_dump(),
            }
        )
        return
    est = the_plan.estimate_1x
    typer.echo(
        f"{analysis.word_count} words, {len(analysis.sentences)} sentences, "
        f"{analysis.paragraph_count} paragraphs  ->  about {format_duration(est.total_seconds)} "
        f"at 1.0x ({est.basis} speaking rate for {voice})"
    )
    for w in analysis.warnings:
        typer.secho(f"  ! {w.message}", fg=typer.colors.YELLOW)
    typer.echo("")
    for i, o in enumerate(the_plan.options):
        star = "*" if i == the_plan.recommended else " "
        label = {"edit_script": "edit script", "adjust_delivery": "adjust delivery",
                 "speed_only": "speed only"}[o.kind]  # fmt: skip
        detail = ""
        if o.kind != "edit_script" and o.speed:
            detail = f"speed {o.speed:.2f}x, pauses x{o.pause_scale:.2f} -> {format_duration(o.resulting_seconds)}"
        typer.echo(f"{star} {label:16} [{o.quality}] {detail} {o.message if o.kind == 'edit_script' else ''}".rstrip())


# ----------------------------------------------------------------- prepare


def _confirm_download(engine_id: str, size_mb: int | None, yes: bool) -> None:
    if yes:
        return
    ok = typer.confirm(
        f"Snipmint needs to download the {engine_id} voice model (~{size_mb} MB). Download now?",
        default=True,
    )
    if not ok:
        typer.echo("Cancelled.")
        raise typer.Exit(1)


@app.command()
@friendly_errors
def prepare(
    engine: Annotated[str, typer.Argument(help="Engine to download/load models for, e.g. kokoro")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask before downloading")] = False,
):
    """Download and load the models an engine needs (one-time)."""
    from .engines.registry import get_engine

    eng = get_engine(engine)
    status = eng.status()
    if not status.available:
        raise SnipmintError(status.reason or f"{engine} is not available.")
    if status.needs_download:
        _confirm_download(engine, status.download_size_mb, yes)
    eng.prepare(_download_progress(True))
    typer.echo(f"{eng.display_name} is ready.")


# ------------------------------------------------------------------- speak


@app.command()
@friendly_errors
def speak(
    script: Annotated[str, typer.Argument(help="Script file ('-' reads stdin)")],
    voice: Annotated[str, typer.Option(help="<engine>:<voice>")] = DEFAULT_VOICE,
    speed: Annotated[float | None, typer.Option(help="0.5 - 2.0 (not with --target)")] = None,
    target: Annotated[str | None, typer.Option(help="Exact length: 60s, 6m, 1:30 ...")] = None,
    energy: Annotated[float, typer.Option(min=0.0, max=1.0, help="0 calm .. 1 hype")] = 0.5,
    output: Annotated[Path | None, typer.Option("-o", "--output", help="Output folder")] = None,
    timing: Annotated[str, typer.Option(help="auto, estimated or aligned")] = "auto",
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Download models without asking")] = False,
    json_out: Annotated[bool, typer.Option("--json")] = False,
):
    """Narrate a script: writes voice.mp3, voice.wav, words.json and captions.srt."""
    from .engines.registry import resolve_voice
    from .jobs.workspace import Workspace
    from .pipeline.speak import speak as run_speak
    from .script import format_duration, parse_duration

    if speed is not None and target:
        raise ScriptError("Use either --speed or --target, not both.")
    if timing not in ("auto", "estimated", "aligned"):
        raise ScriptError("--timing must be auto, estimated or aligned.")
    text = read_script(script)
    selection = VoiceSelection.parse(voice, speed=speed or 1.0, energy=energy)
    target_s = parse_duration(target) if target else None

    engine, _ = resolve_voice(selection)
    status = engine.status()
    if status.needs_download:
        _confirm_download(engine.id, status.download_size_mb, yes)
        engine.prepare(_download_progress(not json_out))

    ws = Workspace(output) if output else Workspace.create()
    result = run_speak(
        text, selection, ws, target_seconds=target_s, timing=timing,  # type: ignore[arg-type]
        progress=_progress_printer(not json_out and sys.stderr.isatty()),
    )  # fmt: skip
    if json_out:
        _emit_json({"folder": str(ws.path), **result.model_dump(exclude={"words"}),
                    "words": len(result.words)})  # fmt: skip
        return
    typer.echo(f"Done: {format_duration(result.duration)} of narration in {ws.path}")
    for name in ("voice.mp3", "voice.wav", "words.json", "captions.srt"):
        typer.echo(f"  {ws.file(name)}")
    if result.note:
        typer.secho(result.note, fg=typer.colors.YELLOW)


# -------------------------------------------------------------------- make

VIDEO_SUFFIXES = {".mp4", ".webm", ".mov"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
BACKGROUND_VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}


def _parse_overrides(pairs: list[str] | None) -> dict[str, Any]:
    """``--set colors.highlight=#FF0000`` -> nested dict (values parsed as JSON when possible)."""
    out: dict[str, Any] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ScriptError(f"--set expects key=value, got '{pair}'.")
        key, raw = pair.split("=", 1)
        try:
            value: Any = json.loads(raw)
        except ValueError:
            value = raw
        node = out
        parts = key.strip().split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return out


@app.command()
@friendly_errors
def make(
    script: Annotated[str | None, typer.Argument(help="Script file ('-' reads stdin)")] = None,
    project_file: Annotated[
        Path | None, typer.Option("--project", help="A .snipmint.json project file")
    ] = None,
    voice: Annotated[str, typer.Option(help="<engine>:<voice>")] = DEFAULT_VOICE,
    speed: Annotated[float | None, typer.Option(help="0.5 - 2.0 (not with --target)")] = None,
    target: Annotated[str | None, typer.Option(help="Exact length: 60s, 6m, 1:30 ...")] = None,
    energy: Annotated[float, typer.Option(min=0.0, max=1.0, help="0 calm .. 1 hype")] = 0.5,
    format: Annotated[str, typer.Option(help="reels, youtube, square, portrait, audio")] = "reels",
    style: Annotated[str, typer.Option(help="Caption preset (see `snipmint styles`)")] = "bold-pop",
    font: Annotated[str | None, typer.Option(help="Font id (see `snipmint fonts`)")] = None,
    set_: Annotated[
        list[str] | None, typer.Option("--set", help="Style tweak, e.g. colors.highlight=#FF4B4B")
    ] = None,
    background: Annotated[
        str, typer.Option(help="#RRGGBB colour, an image/video path, or 'transparent'")
    ] = "#101010",
    quality: Annotated[str, typer.Option(help="fast, balanced or high")] = "balanced",
    output: Annotated[
        Path | None, typer.Option("-o", "--output", help="video.mp4 (a file) or a folder")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Download models without asking")] = False,
    json_out: Annotated[bool, typer.Option("--json")] = False,
):
    """Make the finished video: narration, word-synced captions, one ffmpeg render."""
    from .engines.registry import resolve_voice
    from .jobs.workspace import Workspace
    from .pipeline.make import export_name, make_video
    from .project import (
        BackgroundConfig,
        CaptionsConfig,
        DurationConfig,
        ExportConfig,
        ProjectConfig,
    )
    from .script import format_duration, parse_duration

    to_file = output is not None and output.suffix.lower() in VIDEO_SUFFIXES
    if project_file:
        project = ProjectConfig.load(project_file)
    else:
        if script is None:
            raise ScriptError("Give a script file, or --project file.snipmint.json.")
        if speed is not None and target:
            raise ScriptError("Use either --speed or --target, not both.")
        if quality not in ("fast", "balanced", "high"):
            raise ScriptError("--quality must be fast, balanced or high.")
        suffix = Path(background).suffix.lower()
        if background.lower() == "transparent":
            bg = BackgroundConfig(type="transparent")
        elif suffix in IMAGE_SUFFIXES:
            bg = BackgroundConfig(type="image", path=background)
        elif suffix in BACKGROUND_VIDEO_SUFFIXES:
            bg = BackgroundConfig(type="video", path=background)
        else:
            bg = BackgroundConfig(type="solid", color=background)
        container = output.suffix.lower().lstrip(".") if to_file and output else "mp4"
        project = ProjectConfig(
            script=read_script(script),
            duration=DurationConfig(target_seconds=parse_duration(target) if target else None),
            voice=VoiceSelection.parse(voice, speed=speed or 1.0, energy=energy),
            format=format,
            captions=CaptionsConfig(preset=style, font=font, overrides=_parse_overrides(set_)),
            background=bg,
            export=ExportConfig(video=container, quality=quality),  # type: ignore[arg-type]
        )

    engine, _ = resolve_voice(project.voice)
    status = engine.status()
    if status.needs_download:
        _confirm_download(engine.id, status.download_size_mb, yes)
        engine.prepare(_download_progress(not json_out))

    ws = Workspace(output) if output and not to_file else Workspace.create()
    result = make_video(project, ws, progress=_progress_printer(not json_out and sys.stderr.isatty()))

    final = result.video
    if final and output and to_file:
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(final, output)
        final = output
    if json_out:
        _emit_json({**result.model_dump(), "video": str(final) if final else None})
        return
    typer.echo(f"Done: {format_duration(result.duration)}")
    if final:
        typer.echo(f"  video: {final}")
        name = export_name(project.script, project.format, project.export.video)
        typer.echo(f"  suggested name: {name}")
    typer.echo(f"  folder: {result.folder}")
    for w in result.warnings:
        typer.secho(f"  ! {w}", fg=typer.colors.YELLOW)
    if result.note:
        typer.secho(result.note, fg=typer.colors.YELLOW)


@app.command()
@friendly_errors
def preview(
    script: Annotated[str, typer.Argument(help="Script file ('-' reads stdin)")],
    output: Annotated[Path, typer.Option("-o", "--output", help="PNG to write")] = Path("preview.png"),
    format: Annotated[str, typer.Option()] = "reels",
    style: Annotated[str, typer.Option()] = "bold-pop",
    font: Annotated[str | None, typer.Option()] = None,
    set_: Annotated[list[str] | None, typer.Option("--set")] = None,
):
    """Render one caption frame (no voice needed) to check how a style looks."""
    from .pipeline.make import preview_frame
    from .project import CaptionsConfig, ProjectConfig

    project = ProjectConfig(
        script=read_script(script),
        format=format,
        captions=CaptionsConfig(preset=style, font=font, overrides=_parse_overrides(set_)),
    )
    output.write_bytes(preview_frame(project))
    typer.echo(f"Wrote {output}")


@app.command()
@friendly_errors
def fonts(json_out: Annotated[bool, typer.Option("--json")] = False):
    """List caption fonts (use the id with --font)."""
    from .captions.fonts import list_fonts

    found = list_fonts()
    if json_out:
        _emit_json([f.public() for f in found])
        return
    typer.echo(f"{'ID':30} {'FAMILY':24} {'LICENSE':10} TAGS")
    for f in found:
        typer.echo(f"{f.id:30} {f.display_name[:24]:24} {f.license:10} {', '.join(f.tags)}")


@app.command()
@friendly_errors
def styles(json_out: Annotated[bool, typer.Option("--json")] = False):
    """List caption style presets (use the id with --style)."""
    from .captions.styles import list_styles

    found = list_styles()
    if json_out:
        _emit_json([s.model_dump(by_alias=True) for s in found])
        return
    for s in found:
        typer.echo(f"{s.id:18} {s.name:20} font {s.font.id}, {s.layout.words_per_chunk} words/caption")


@app.command()
@friendly_errors
def formats(json_out: Annotated[bool, typer.Option("--json")] = False):
    """List video formats (use the id with --format)."""
    from .captions.formats import list_formats

    found = list_formats()
    if json_out:
        _emit_json([f.model_dump() for f in found])
        return
    for f in found:
        size = f"{f.width}x{f.height}" if f.is_video else "audio only"
        typer.echo(f"{f.id:10} {f.name:28} {size}")


# ------------------------------------------------------------------- serve


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


@app.command()
@friendly_errors
def serve(
    host: Annotated[str | None, typer.Option(help="Bind address (default 127.0.0.1)")] = None,
    port: Annotated[int | None, typer.Option(help="Port (default 8000)")] = None,
    open_browser: Annotated[bool, typer.Option("--open", help="Open the app in your browser")] = False,
    dev: Annotated[bool, typer.Option("--dev", help="Allow the Vite dev server (CORS)")] = False,
    demo: Annotated[
        bool, typer.Option("--demo", help="Public demo mode: short scripts, no uploads or cloning")
    ] = False,
):
    """Start the Snipmint web app and API."""
    import threading
    import webbrowser

    from .errors import DependencyMissing

    try:
        import uvicorn

        from .server import create_app
    except ImportError as exc:
        raise DependencyMissing("The web server", 'Install with: pip install "snipmint[server]"') from exc

    settings = config.load_settings()
    bind = host or settings.host
    number = port or settings.port
    token = settings.token
    if not _is_loopback(bind) and not demo:
        if not token:
            raise SnipmintError(
                f"Binding to {bind} exposes Snipmint to your network. Set SNIPMINT_TOKEN to a "
                "secret first (the app then asks for it), or use --host 127.0.0.1."
            )
        typer.secho(f"Warning: listening on {bind}; anyone with the token can use this server.",
                    fg=typer.colors.YELLOW, err=True)  # fmt: skip
    url = f"http://{'127.0.0.1' if _is_loopback(bind) else bind}:{number}"
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url + (f"/?token={token}" if token else ""))).start()
    typer.echo(f"Snipmint is running at {url}  (Ctrl+C to stop)")
    uvicorn.run(create_app(dev=dev, token=token, demo=demo), host=bind, port=number, log_level="warning")


# ------------------------------------------------------------------- clone


@app.command()
@friendly_errors
def clone(
    recording: Annotated[Path, typer.Argument(help="A recording of your voice: WAV, MP3, M4A...")],
    name: Annotated[str, typer.Option("--name", help="What to call this voice")],
    language: Annotated[str, typer.Option(help="Language of the recording, e.g. en or hi")] = "en",
    i_have_consent: Annotated[
        bool, typer.Option("--i-have-consent", help="Confirm the voice is yours or you have permission")
    ] = False,
    json_out: Annotated[bool, typer.Option("--json")] = False,
):
    """Save a recording as a cloned voice (zero-shot: no training, takes a few seconds)."""
    from .voices import CONSENT_TEXT, create_profile

    if not recording.is_file():
        raise ScriptError(f"Recording not found: {recording}")
    if not i_have_consent:
        typer.echo(CONSENT_TEXT)
        if not typer.confirm("Do you confirm this?", default=False):
            raise ScriptError("Voice cloning needs your confirmation about whose voice this is.")
    profile, report = create_profile(recording.read_bytes(), name, True, language)
    if json_out:
        _emit_json({"profile": profile.model_dump(), "quality": report.model_dump()})
        return
    for c in report.checks:
        mark = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}[c.status]
        typer.echo(f"[{mark}] {c.label}: {c.detail}")
    typer.echo(f"Saved voice '{profile.name}'. Use it with:  --voice chatterbox:{profile.id}")
    from .engines.chatterbox import ChatterboxEngine

    if not ChatterboxEngine().status().available:
        typer.secho('To speak with it, install the cloning engine: pip install "snipmint[clone]"',
                    fg=typer.colors.YELLOW)  # fmt: skip


# ------------------------------------------------------------------- clean


@app.command()
@friendly_errors
def clean(
    keep: Annotated[int | None, typer.Option(help="Always keep this many newest jobs")] = None,
    days: Annotated[float, typer.Option(help="Only delete jobs older than this")] = 7.0,
):
    """Delete old job folders and stale cached sentences."""
    from .jobs.workspace import clean_jobs
    from .pipeline.cache import SentenceCache

    removed = clean_jobs(keep=keep, max_age_days=days)
    cache_removed = SentenceCache().clean(max_age_days=30)
    typer.echo(f"Removed {len(removed)} old job folders and {cache_removed} cached sentences.")


if __name__ == "__main__":  # pragma: no cover
    app()
