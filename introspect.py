"""Print the google-genai SDK surface for session resumption (no network, no key)."""

import inspect
from importlib.metadata import version

from google.genai import live, types


def fields(model) -> dict:
    return {k: str(v.annotation).replace("typing.", "") for k, v in model.model_fields.items()}


def show(name: str) -> None:
    cls = getattr(types, name, None)
    print(f"\n== types.{name} ==")
    if cls is None:
        print("MISSING")
        return
    for k, ann in fields(cls).items():
        desc = (cls.model_fields[k].description or "").replace("\n", " ")
        print(f"- {k}: {ann}\n    {desc}")


print("google-genai", version("google-genai"))
for n in ("SessionResumptionConfig", "LiveServerSessionResumptionUpdate", "LiveServerGoAway",
          "GoAway", "ContextWindowCompressionConfig", "SlidingWindow"):
    show(n)
print("\n== LiveConnectConfig fields (resumption-related) ==")
for k in types.LiveConnectConfig.model_fields:
    if any(s in k for s in ("resum", "compress", "session", "window")):
        print("-", k, types.LiveConnectConfig.model_fields[k].annotation)
print("\n== LiveServerMessage fields ==")
print(", ".join(types.LiveServerMessage.model_fields))
print("\n== AsyncSession methods ==")
for name in ("send_realtime_input", "send_client_content", "send_tool_response", "receive", "close"):
    fn = getattr(live.AsyncSession, name, None)
    print(f"{name}{inspect.signature(fn)}" if fn else f"{name}: MISSING")
print("AsyncLive.connect", inspect.signature(live.AsyncLive.connect))
