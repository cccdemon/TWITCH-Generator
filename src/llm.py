"""Provider abstraction for structured model calls (Anthropic tool-use | Ollama JSON schema).

Every model call in the pipeline (analyze, refine, vision triage/confirm, facecam)
goes through `structured()`. The caller describes the request in neutral terms
(system prompt, a list of TextPart/ImagePart, a JSON schema) and gets back the
parsed object as a dict. Which backend answers is decided by `llm.provider` in
config.yaml:

  anthropic  (default, prod)  — exactly the historical behaviour: tool-use with a
             forced tool_choice, model name from the calling stage's config
             section, `meter.add(model, usage)`.
  ollama     — POST {base_url}/api/chat with `format=<schema>` (structured
             outputs), `think: false`, temperature 0, per-role num_ctx, images as
             base64 in `message.images`. Model names come from `llm.ollama.*`;
             the Claude names in the stage sections are ignored.

Config (all optional; defaults reproduce prod):

  llm:
    provider: anthropic | ollama
    ollama:
      base_url: http://ollama:11434
      text_model: qwen3:14b
      vision_model: qwen3-vl:8b-instruct
      num_ctx: {text: 32768, vision: 32768}
      timeout_seconds: 600
"""
from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import metrics
from .config import env

PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OLLAMA = "ollama"

_OLLAMA_DEFAULTS = {
    "base_url": "http://ollama:11434",
    "text_model": "qwen3:14b",
    # qwen3-vl:8b (no suffix) is the THINKING variant in Ollama and ignores think:false;
    # its reasoning eats num_predict and truncates the JSON. Always use -instruct.
    "vision_model": "qwen3-vl:8b-instruct",
    "num_ctx": {"text": 32768, "vision": 32768},
    "timeout_seconds": 600,
}
_JSON_ONLY = "Return ONLY the JSON object."
_CHARS_PER_TOKEN = 3.0          # conservative estimate for the num_ctx warning
# Measured on qwen3-vl:8b: a 640x360 JPEG costs ~1085 prompt tokens, a 768px burst
# frame ~1500. Images cannot be truncated, so a batch over num_ctx is a hard 400.
_IMAGE_TOKENS = 1100
_CTX_WARN_FRACTION = 0.9
_CTX_IMAGE_BUDGET = 0.85        # share of num_ctx that images may fill in one call


@dataclass
class TextPart:
    text: str


@dataclass
class ImagePart:
    path: Path


Part = TextPart | ImagePart


class LLMError(RuntimeError):
    """A provider returned no usable structured object."""


@dataclass
class _Usage:
    """Duck-types anthropic's Usage for pricing.CostMeter.add."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


# ------------------------------------------------------------------ config ----
def provider(cfg: dict | None) -> str:
    p = str(((cfg or {}).get("llm") or {}).get("provider") or PROVIDER_ANTHROPIC).lower()
    if p not in (PROVIDER_ANTHROPIC, PROVIDER_OLLAMA):
        raise ValueError(f"llm.provider must be 'anthropic' or 'ollama', got {p!r}")
    return p


def ollama_cfg(cfg: dict | None) -> dict:
    o = dict(_OLLAMA_DEFAULTS)
    o.update(((cfg or {}).get("llm") or {}).get("ollama") or {})
    ctx = dict(_OLLAMA_DEFAULTS["num_ctx"])
    ctx.update(o.get("num_ctx") or {})
    o["num_ctx"] = ctx
    return o


def model_for(role: str, cfg: dict | None, claude_model: str) -> str:
    """Effective model name for `role` under the configured provider.

    Stages store this in their cache params, so a provider switch invalidates
    model-dependent caches (vision_triage.json, vision.json) cleanly.
    """
    if provider(cfg) == PROVIDER_OLLAMA:
        o = ollama_cfg(cfg)
        return str(o["vision_model"] if role == "vision" else o["text_model"])
    return claude_model


def vision_concurrency(cfg: dict | None, configured: int) -> int:
    """Ollama serialises per model anyway; parallel requests only risk timeouts."""
    return 1 if provider(cfg) == PROVIDER_OLLAMA else max(1, int(configured))


def images_per_call(cfg: dict | None, requested: int, *, image_tokens: int = _IMAGE_TOKENS,
                    text_tokens: int = 1500) -> int:
    """Cap the images in one vision call so prompt + images fit llm.ollama.num_ctx.vision.

    Anthropic has no such limit → `requested` unchanged. Under ollama the cap is
    derived from num_ctx; the caller splits its batches accordingly.
    """
    requested = max(1, int(requested))
    if provider(cfg) != PROVIDER_OLLAMA:
        return requested
    num_ctx = int(ollama_cfg(cfg)["num_ctx"]["vision"])
    fit = int((num_ctx * _CTX_IMAGE_BUDGET - text_tokens) // image_tokens)
    return max(1, min(requested, fit))


def describe(cfg: dict | None) -> str:
    """One log line for the pipeline start: provider, models, whisper backend."""
    cfg = cfg or {}
    w = cfg.get("whisper") or {}
    backend = w.get("backend", "faster-whisper")
    wmodel = w.get("model", "small")
    if provider(cfg) == PROVIDER_OLLAMA:
        o = ollama_cfg(cfg)
        return (f"llm=ollama text={o['text_model']} vision={o['vision_model']} "
                f"num_ctx={o['num_ctx']['text']}/{o['num_ctx']['vision']} @ {o['base_url']} · "
                f"whisper={backend} model={wmodel}")
    a = cfg.get("analyze") or {}
    v = cfg.get("vision") or {}
    r = cfg.get("refine") or {}
    return (f"llm=anthropic text={a.get('model', 'claude-sonnet-4-6')} "
            f"refine={r.get('model') or a.get('model', 'claude-sonnet-4-6')} "
            f"vision={v.get('triage_model') or v.get('model')}/{v.get('model')} · "
            f"whisper={backend} model={wmodel}")


# --------------------------------------------------------------- public API ----
def structured(
    *,
    role: str,
    system: str | None,
    content: list[Part],
    schema: dict,
    tool_name: str,
    cfg: dict | None,
    meter=None,
    max_tokens: int = 2048,
    model: str | None = None,
    tool_description: str = "",
    log=print,
) -> dict:
    """One structured model call; returns the parsed object (a dict), or raises.

    role             "text" | "vision" — selects the Ollama model / num_ctx.
    model            the Claude model name the calling stage configured; used by
                     the anthropic provider, ignored by ollama.
    tool_name/desc   anthropic tool definition (schema = its input_schema).
    meter            pricing.CostMeter; usage is added under the effective model
                     name (ollama models are prefixed "ollama/" → $0).
    """
    if role not in ("text", "vision"):
        raise ValueError(f"role must be 'text' or 'vision', got {role!r}")
    if provider(cfg) == PROVIDER_OLLAMA:
        return _ollama(role=role, system=system, content=content, schema=schema,
                       cfg=cfg, meter=meter, max_tokens=max_tokens, log=log)
    if not model:
        raise ValueError("anthropic provider needs the stage's model name")
    return _anthropic(model=model, system=system, content=content, schema=schema,
                      tool_name=tool_name, tool_description=tool_description,
                      meter=meter, max_tokens=max_tokens)


# ---------------------------------------------------------------- anthropic ----
def _img_b64(path: Path) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


def _anthropic_blocks(content: list[Part]) -> list[dict]:
    blocks: list[dict] = []
    for part in content:
        if isinstance(part, TextPart):
            blocks.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePart):
            blocks.append({"type": "image",
                           "source": {"type": "base64", "media_type": "image/jpeg",
                                      "data": _img_b64(part.path)}})
        else:
            raise TypeError(f"unsupported part: {type(part).__name__}")
    return blocks


def _anthropic(*, model: str, system: str | None, content: list[Part], schema: dict,
               tool_name: str, tool_description: str, meter, max_tokens: int) -> dict:
    from anthropic import Anthropic  # lazy: keeps the module importable without the SDK
    client = Anthropic(api_key=env("ANTHROPIC_API_KEY", required=True))
    tool: dict[str, Any] = {"name": tool_name, "input_schema": schema}
    if tool_description:
        tool["description"] = tool_description
    kwargs: dict[str, Any] = {}
    if system:
        kwargs["system"] = system
    t0 = time.time()
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool_name},
        messages=[{"role": "user", "content": _anthropic_blocks(content)}],
        **kwargs,
    )
    if meter is not None:
        meter.add(model, msg.usage)
    m = metrics.current()
    if m is not None:
        u = msg.usage
        m.llm_call(model=model,
                   prompt_tokens=int(getattr(u, "input_tokens", 0) or 0)
                   + int(getattr(u, "cache_read_input_tokens", 0) or 0)
                   + int(getattr(u, "cache_creation_input_tokens", 0) or 0),
                   gen_tokens=int(getattr(u, "output_tokens", 0) or 0),
                   wall_s=time.time() - t0,
                   usd=getattr(meter, "usd", None) if meter is not None else None)
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and isinstance(block.input, dict):
            return block.input
    return {}


# ------------------------------------------------------------------- ollama ----
_first_call_lock = threading.Lock()
_first_call_done = False


def _ollama_messages(system: str | None, content: list[Part]) -> list[dict]:
    texts: list[str] = []
    images: list[str] = []
    for part in content:
        if isinstance(part, TextPart):
            texts.append(part.text)
        elif isinstance(part, ImagePart):
            images.append(_img_b64(part.path))
        else:
            raise TypeError(f"unsupported part: {type(part).__name__}")
    sys_text = f"{system.rstrip()} {_JSON_ONLY}" if system else _JSON_ONLY
    user: dict[str, Any] = {"role": "user", "content": "\n".join(texts)}
    if images:
        user["images"] = images
    return [{"role": "system", "content": sys_text}, user]


def _estimate_tokens(messages: list[dict]) -> int:
    chars = sum(len(m.get("content") or "") for m in messages)
    imgs = sum(len(m.get("images") or []) for m in messages)
    return int(chars / _CHARS_PER_TOKEN) + imgs * _IMAGE_TOKENS


def _ollama(*, role: str, system: str | None, content: list[Part], schema: dict,
            cfg: dict | None, meter, max_tokens: int, log) -> dict:
    import httpx

    o = ollama_cfg(cfg)
    model = str(o["vision_model"] if role == "vision" else o["text_model"])
    num_ctx = int(o["num_ctx"][role])
    timeout = float(o.get("timeout_seconds") or 600)
    url = str(o["base_url"]).rstrip("/") + "/api/chat"

    messages = _ollama_messages(system, content)
    est = _estimate_tokens(messages)
    if est > _CTX_WARN_FRACTION * num_ctx:
        log(f"[yellow]llm: prompt ≈{est} tokens vs num_ctx {num_ctx} for role={role} — "
            f"Ollama will truncate silently; raise llm.ollama.num_ctx.{role}")

    # Reasoning: off by default (schema-constrained output gains nothing from it on
    # Qwen3). Reasoning-only models (gpt-oss) ignore `false`; for them set
    # llm.ollama.think to "low"|"medium"|"high" and the thinking budget is added
    # on top of max_tokens so the reasoning cannot starve the answer.
    think = o.get("think", False)
    num_predict = int(max_tokens)
    if think not in (False, None, "false"):
        num_predict += int(o.get("think_budget_tokens", 8192))
    body: dict[str, Any] = {
        "model": model,
        "stream": False,
        "think": think if think not in (None, "false") else False,
        "format": schema,
        "options": {"temperature": 0, "num_ctx": num_ctx, "num_predict": num_predict},
        "messages": messages,
    }

    last_err: Exception | None = None
    for attempt in range(2):  # one retry on malformed JSON / transient HTTP error
        t0 = time.time()
        try:
            with httpx.Client(timeout=httpx.Timeout(timeout, connect=30.0)) as client:
                resp = client.post(url, json=body)
                if resp.status_code == 400 and "think" in resp.text and "think" in body:
                    # model without thinking support: drop the flag and resend
                    body.pop("think")
                    resp = client.post(url, json=body)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as e:
            last_err = e
            body = ""
            resp_obj = getattr(e, "response", None)
            if resp_obj is not None:
                try:
                    body = f" — server said: {resp_obj.text[:300]}"
                except Exception:  # noqa: BLE001
                    body = ""
            log(f"[yellow]llm: ollama request failed ({type(e).__name__}: {e}){body} — attempt {attempt + 1}/2")
            if resp_obj is not None and resp_obj.status_code == 400:
                # a 400 is deterministic (context overflow, bad schema) — retrying is pointless
                last_err = LLMError(f"ollama/{model} 400: {resp_obj.text[:300]}")
                break
            continue
        wall = time.time() - t0

        usage = _Usage(input_tokens=int(data.get("prompt_eval_count") or 0),
                       output_tokens=int(data.get("eval_count") or 0))
        if meter is not None:
            meter.add(f"ollama/{model}", usage)
        _log_speed(data, model, wall, est, log)
        m = metrics.current()
        if m is not None:
            m.llm_call(model=f"ollama/{model}", prompt_tokens=usage.input_tokens,
                       gen_tokens=usage.output_tokens, wall_s=wall,
                       prefill_s=(data.get("prompt_eval_duration") or 0) / 1e9 or None,
                       gen_s=(data.get("eval_duration") or 0) / 1e9 or None,
                       usd=getattr(meter, "usd", None) if meter is not None else None)

        msg_obj = data.get("message") or {}
        text = msg_obj.get("content") or ""
        thinking = msg_obj.get("thinking") or ""
        if thinking and think in (False, None, "false"):
            _warn_thinking(model, len(thinking), len(text), log)
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as e:
            last_err = e
            log(f"[yellow]llm: ollama returned non-JSON ({e}) — attempt {attempt + 1}/2; "
                f"head={text[:120]!r}")
            continue
        if not isinstance(obj, dict):
            last_err = LLMError(f"ollama returned {type(obj).__name__}, expected object")
            continue
        return obj
    raise LLMError(f"ollama/{model}: {last_err}")


_thinking_warned: set[str] = set()


def _warn_thinking(model: str, think_chars: int, content_chars: int, log) -> None:
    """Once per model: the model produced a thinking block despite think=false.

    Ollama's "thinking" model variants (e.g. qwen3-vl:8b, renderer qwen3-vl-thinking)
    reason anyway; the reasoning counts against num_predict and silently truncates
    the structured answer. The fix is the -instruct tag, not a bigger limit.
    """
    with _first_call_lock:
        if model in _thinking_warned:
            return
        _thinking_warned.add(model)
    log(f"[yellow]llm: {model} returned {think_chars} chars of thinking next to {content_chars} chars "
        "of answer although think=false — this model reasons regardless. Either use its "
        "-instruct tag, or set llm.ollama.think: medium (plus think_budget_tokens) so the "
        "reasoning gets its own budget instead of eating the JSON answer")


def _log_speed(data: dict, model: str, wall: float, est: int, log) -> None:
    """tok/s on the first call of a run (GPU proof in the job log), truncation hint always."""
    global _first_call_done
    pe = int(data.get("prompt_eval_count") or 0)
    ec = int(data.get("eval_count") or 0)
    ed = (data.get("eval_duration") or 0) / 1e9
    ped = (data.get("prompt_eval_duration") or 0) / 1e9
    load = (data.get("load_duration") or 0) / 1e9
    with _first_call_lock:
        first, _first_call_done = (not _first_call_done), True
    if first:
        gen = f"{ec / ed:.1f} tok/s" if ed else "n/a"
        pp = f"{pe / ped:.0f} tok/s" if ped else "n/a"
        log(f"[cyan]llm: first ollama call[/] model={model} prompt={pe} tok (prefill {pp}) "
            f"gen={ec} tok ({gen}) load={load:.1f}s wall={wall:.1f}s "
            "— GPU reference: qwen3:14b ≈ 25-40 tok/s, CPU ≈ 2-4 tok/s")
    if est and pe and pe < 0.5 * est:
        log(f"[yellow]llm: prompt_eval_count {pe} ≪ estimate {est} — prompt was probably "
            "truncated to num_ctx (or heavily cached); check llm.ollama.num_ctx")
