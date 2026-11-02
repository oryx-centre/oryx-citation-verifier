"""Provider fallback router — STAGED, NOT wired into production.

Design from business-plan-v1-30oct.md. Rules enforced here:
- Order: zai -> groq -> mistral -> openrouter_free -> google_ai_studio.
- A run is labelled with the provider that actually produced it; a
  failure never silently becomes another provider's result.
- Google AI Studio free tier is LAST and is EXCLUDED when
  client_data=True (free-tier content may be used to improve
  Google's products), so client jobs can never fall through to it.
- Every attempt counts against the daily job budget (callers pass
  jobs_used / daily_budget); the router never hides quota use.

Providers are callables taking (question) and returning a string, or
raising. Real provider clients are NOT in this module — wire them in
only after a local end-to-end pass, and never deploy an untested
router to the public beta.
"""

PROVIDER_ORDER = ["zai", "groq", "mistral", "openrouter_free", "google_ai_studio"]


class AllProvidersFailed(Exception):
    pass


def route(question, providers, client_data=False, jobs_used=0, daily_budget=3):
    """Try providers in order. Returns (provider_name, result, attempts).

    providers: dict name -> callable. Missing names are skipped.
    Raises AllProvidersFailed with the attempt log attached.
    """
    if jobs_used >= daily_budget:
        raise AllProvidersFailed("daily job budget already used")
    attempts = []
    for name in PROVIDER_ORDER:
        if client_data and name == "google_ai_studio":
            attempts.append((name, "excluded: client data"))
            continue
        fn = providers.get(name)
        if fn is None:
            attempts.append((name, "not configured"))
            continue
        try:
            result = fn(question)
        except Exception as exc:  # noqa: BLE001 - provider errors are the point
            attempts.append((name, f"failed: {type(exc).__name__}"))
            continue
        return name, result, attempts
    raise AllProvidersFailed(f"all providers failed or excluded: {attempts}")
