from __future__ import annotations

import csv
import json
import os
import smtplib
from string import Formatter
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, Dict

import requests

ActionFn = Callable[[Dict[str, Any], Dict[str, Any]], Any]


class SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _render_template(template: str, context: Dict[str, Any]) -> str:
    formatter = Formatter()
    for _, field_name, _, _ in formatter.parse(template):
        if field_name:
            context.setdefault(field_name, "")
    return template.format_map(SafeDict(context))


def _extract_json_object(raw_text: str) -> Dict[str, Any]:
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("La respuesta del modelo no contiene JSON valido")
    return json.loads(text[start : end + 1])


def action_set_values(context: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    values = params.get("values", {})
    if not isinstance(values, dict):
        raise ValueError("'values' debe ser un objeto/dict")
    context.update(values)
    return values


def action_template(context: Dict[str, Any], params: Dict[str, Any]) -> str:
    template = params.get("template", "")
    if not isinstance(template, str):
        raise ValueError("'template' debe ser string")
    return _render_template(template, context)


def action_http_get(context: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    url = params.get("url")
    timeout = params.get("timeout", 10)
    if not url or not isinstance(url, str):
        raise ValueError("'url' es obligatorio y debe ser string")

    response = requests.get(url, timeout=timeout)
    response.raise_for_status()

    return {
        "status_code": response.status_code,
        "headers": dict(response.headers),
        "text": response.text[:2000],
    }


def action_log(context: Dict[str, Any], params: Dict[str, Any]) -> str:
    message = params.get("message", "")
    rendered = message.format_map(SafeDict(context)) if isinstance(message, str) else str(message)
    print(f"[flow-log] {rendered}")
    return rendered


def action_validate_required(context: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    fields = params.get("fields", [])
    if not isinstance(fields, list):
        raise ValueError("'fields' debe ser lista")

    missing = [field for field in fields if not context.get(field)]
    if missing:
        raise ValueError(f"Faltan campos obligatorios: {', '.join(missing)}")

    return {"ok": True, "checked": fields}


def action_github_models_shopping_list(context: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    token = os.getenv("GH_MODELS_TOKEN")
    if not token:
        raise ValueError("Configura GH_MODELS_TOKEN para usar GitHub Models")

    endpoint = os.getenv("GH_MODELS_ENDPOINT", "https://models.inference.ai.azure.com/chat/completions")
    model = params.get("model") or os.getenv("GH_MODELS_MODEL", "gpt-4o-mini")
    timeout = int(params.get("timeout", 45))

    diet_text = str(context.get("diet_text", "")).strip()
    if not diet_text:
        raise ValueError("No hay dieta para analizar en 'diet_text'")

    system_prompt = (
        "Eres un asistente nutricional. Convierte una dieta en una lista de compras estructurada. "
        "Responde SOLO JSON con este formato: "
        "{\"items\": [{\"name\": str, \"quantity\": str, \"category\": str}], \"notes\": str, \"plain_list\": str}."
    )

    payload = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"Dieta a analizar:\n{diet_text}\n\nGenera lista de compras semanal consolidada.",
            },
        ],
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
    if response.status_code >= 400:
        error_text = response.text[:1200]
        raise ValueError(
            f"GitHub Models devolvio {response.status_code}. Revisa GH_MODELS_MODEL/token/permisos. "
            f"Detalle: {error_text}"
        )
    data = response.json()

    content = data["choices"][0]["message"]["content"]
    parsed = _extract_json_object(content)
    items = parsed.get("items") or []
    if not isinstance(items, list) or len(items) == 0:
        raise ValueError("El modelo no devolvio items de compra")

    plain_list = parsed.get("plain_list")
    if not isinstance(plain_list, str) or not plain_list.strip():
        lines = []
        for item in items:
            name = item.get("name", "producto")
            qty = item.get("quantity", "cantidad no indicada")
            category = item.get("category", "general")
            lines.append(f"- {name}: {qty} ({category})")
        plain_list = "\n".join(lines)

    context["shopping_items"] = items
    context["shopping_list_text"] = plain_list
    context["shopping_notes"] = parsed.get("notes", "")
    context["status"] = "LISTA_GENERADA"

    return {
        "items_count": len(items),
        "plain_list": plain_list,
        "notes": context.get("shopping_notes", ""),
    }


def action_save_csv(context: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    csv_path = Path(str(params.get("path", "data/diet_shopping_runs.csv")))
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    row = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "email": str(context.get("email", "")),
        "diet_text": str(context.get("diet_text", "")),
        "shopping_list_text": str(context.get("shopping_list_text", "")),
        "shopping_items_json": json.dumps(context.get("shopping_items", []), ensure_ascii=False),
        "shopping_notes": str(context.get("shopping_notes", "")),
        "status": str(params.get("status", context.get("status", "GUARDADO"))),
    }

    headers = list(row.keys())
    exists = csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        if not exists:
            writer.writeheader()
        writer.writerow(row)

    context["csv_path"] = str(csv_path)
    context["status"] = "GUARDADO_EN_CSV"
    return {"csv_path": str(csv_path), "saved": True}


def action_send_email_smtp(context: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    smtp_host = os.getenv("SMTP_HOST")
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASS")
    smtp_sender = os.getenv("SMTP_SENDER", smtp_user or "")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))

    if not smtp_host or not smtp_user or not smtp_pass or not smtp_sender:
        raise ValueError("Configura SMTP_HOST, SMTP_USER, SMTP_PASS y SMTP_SENDER")

    to_email = str(context.get("email", "")).strip()
    if not to_email:
        raise ValueError("No hay destinatario en 'email'")

    subject_template = str(params.get("subject", "Tu lista de compras"))
    body_template = str(
        params.get(
            "body_template",
            "Hola,\n\nAqui tienes tu lista de compras:\n\n{shopping_list_text}\n\nSaludos.",
        )
    )

    subject = _render_template(subject_template, context)
    body = _render_template(body_template, context)

    message = EmailMessage()
    message["From"] = smtp_sender
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(body)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.send_message(message)

    context["status"] = "EMAIL_ENVIADO"
    return {"sent": True, "to": to_email, "subject": subject}


ACTION_REGISTRY: Dict[str, ActionFn] = {
    "set_values": action_set_values,
    "template": action_template,
    "http_get": action_http_get,
    "log": action_log,
    "validate_required": action_validate_required,
    "github_models_shopping_list": action_github_models_shopping_list,
    "save_csv": action_save_csv,
    "send_email_smtp": action_send_email_smtp,
}
