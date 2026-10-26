"""Shared implementation; source-specific entry points remain compatible."""
from typing import Any, Dict, List, Optional
from src.agent.provider_trace import resolved_model_provider_identity

def resolve_configured_response_provider(
    configured_model: str,
    response_model: str,
    model_list: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Match the actual response model against all deployments of one alias."""
    normalized_configured_model = str(configured_model or "").strip()
    normalized_response_model = str(response_model or "").strip().lower()
    if not normalized_configured_model or not normalized_response_model or not model_list:
        return ""

    for entry in model_list:
        params = entry.get("litellm_params", {}) or {}
        model_name = str(entry.get("model_name") or "").strip()
        if not model_name:
            model_name = str(params.get("model") or "").strip()
        if model_name != normalized_configured_model:
            continue

        deployment_model = str(params.get("model") or "").strip()
        if deployment_model.lower() != normalized_response_model:
            continue

        normalized_deployment_model = deployment_model.lower()
        if normalized_deployment_model.startswith("openai/~") or "openrouter" in normalized_deployment_model:
            return "openrouter"

        _resolved_model, resolved_provider = resolved_model_provider_identity(
            deployment_model,
        )
        if resolved_provider:
            return resolved_provider
    return ""
