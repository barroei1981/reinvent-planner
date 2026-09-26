"""Generate README screenshots using Playwright + realistic mock data."""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.html_views import html_schedule, html_slot_alternatives, open_in_browser

# ── Realistic mock schedule ────────────────────────────────────────────────────

MOCK_SCHEDULE = [
    # Dec 2 ─────────────────────────────────────────────
    {
        "event_id": "evt001",
        "title": "Emerging multi-agent patterns for complex financial workflows",
        "description": "Explore cutting-edge multi-agent architectures designed for high-stakes financial environments. Learn how teams at AWS and leading financial institutions are building robust agent pipelines with Amazon Bedrock that handle trade reconciliation, risk assessment, and regulatory reporting at scale.",
        "start_date": "2026-12-02",
        "start_time": "10:00 AM",
        "scheduled_start": "2026-12-02T10:00:00",
        "scheduled_end": "2026-12-02T11:00:00",
        "location": "Venetian | Level 3 | Lando 4220",
        "location_mode": "physical",
        "learning_level": "Advanced",
        "event_type": "Breakout Session",
        "partner_name": None,
        "score": 0.97,
        "registration_url": "https://registration.awsevents.com/flow/awsevents/reinvent26/reg/login",
        "learn_more_url": "https://reinvent.awsevents.com/",
        "is_wishlisted": True,
    },
    {
        "event_id": "evt002",
        "title": "How Prime Video built an AI-assisted creative suite on AWS",
        "description": "Prime Video's engineering team walks through the architecture of their generative AI toolchain for content localisation, metadata generation, and A/B thumbnail testing — all powered by Amazon Bedrock and custom fine-tuned models. Real production numbers included.",
        "start_date": "2026-12-02",
        "start_time": "1:00 PM",
        "scheduled_start": "2026-12-02T13:00:00",
        "scheduled_end": "2026-12-02T14:00:00",
        "location": "Venetian | Level 2 | Titian 2205",
        "location_mode": "physical",
        "learning_level": "Expert",
        "event_type": "Chalk Talk",
        "partner_name": "Prime Video",
        "score": 0.91,
        "registration_url": "https://registration.awsevents.com/flow/awsevents/reinvent26/reg/login",
        "learn_more_url": "https://reinvent.awsevents.com/",
    },
    {
        "event_id": "evt003",
        "title": "Evaluating AI agents for production in financial services",
        "description": "A practical workshop covering eval frameworks, red-teaming strategies, and compliance checkpoints for agentic systems under financial regulations (MiFID II, SEC guidelines). Hands-on labs using Amazon Bedrock Guardrails.",
        "start_date": "2026-12-02",
        "start_time": "3:30 PM",
        "scheduled_start": "2026-12-02T15:30:00",
        "scheduled_end": "2026-12-02T17:30:00",
        "location": "Caesars Forum | Forum 117",
        "location_mode": "physical",
        "learning_level": "Advanced",
        "event_type": "Workshop",
        "partner_name": None,
        "score": 0.88,
        "registration_url": "https://registration.awsevents.com/flow/awsevents/reinvent26/reg/login",
        "learn_more_url": "https://reinvent.awsevents.com/",
    },
    {
        "event_id": "evt003b",
        "title": "Agentic AI governance for regulated industries",
        "description": "Backup session — covers compliance frameworks and governance tooling for agentic AI pipelines in regulated sectors.",
        "start_date": "2026-12-02",
        "start_time": "1:00 PM",
        "scheduled_start": "2026-12-02T13:00:00",
        "scheduled_end": "2026-12-02T14:00:00",
        "location": "Caesars Forum | Forum 120",
        "location_mode": "physical",
        "learning_level": "Advanced",
        "event_type": "Breakout Session",
        "partner_name": None,
        "score": 0.85,
        "registration_url": "https://registration.awsevents.com/flow/awsevents/reinvent26/reg/login",
        "learn_more_url": "https://reinvent.awsevents.com/",
        "backup": True,
        "backup_note": "Backup for: How Prime Video built an AI-assisted creative suite on AWS",
    },
    # Dec 3 ─────────────────────────────────────────────
    {
        "event_id": "evt004",
        "title": "Building production RAG systems that actually work",
        "description": "Beyond naive RAG: chunking strategies, hybrid search, re-ranking with cross-encoders, and context compression. Walk through a real production system serving 10M+ queries/day on Amazon OpenSearch and Bedrock.",
        "start_date": "2026-12-03",
        "start_time": "9:00 AM",
        "scheduled_start": "2026-12-03T09:00:00",
        "scheduled_end": "2026-12-03T10:00:00",
        "location": "Venetian | Level 3 | Lando 4225",
        "location_mode": "physical",
        "learning_level": "Expert",
        "event_type": "Breakout Session",
        "partner_name": None,
        "score": 0.94,
        "registration_url": "https://registration.awsevents.com/flow/awsevents/reinvent26/reg/login",
        "learn_more_url": "https://reinvent.awsevents.com/",
    },
    {
        "event_id": "evt005",
        "title": "SageMaker HyperPod: distributed training at petabyte scale",
        "description": "Deep dive into SageMaker HyperPod's fault-tolerant checkpointing, topology-aware scheduling, and new UltraCluster networking. Live demo training a 70B parameter model with automatic node replacement.",
        "start_date": "2026-12-03",
        "start_time": "11:30 AM",
        "scheduled_start": "2026-12-03T11:30:00",
        "scheduled_end": "2026-12-03T12:30:00",
        "location": "Venetian | Level 3 | Lando 4201",
        "location_mode": "physical",
        "learning_level": "Expert",
        "event_type": "Breakout Session",
        "partner_name": None,
        "score": 0.89,
        "registration_url": "https://registration.awsevents.com/flow/awsevents/reinvent26/reg/login",
        "learn_more_url": "https://reinvent.awsevents.com/",
    },
    {
        "event_id": "evt006",
        "title": "DevSecOps at scale: shifting security left with Amazon Q",
        "description": "See how Amazon Q Developer's new security scanning, IaC remediation, and pull-request review features integrate into GitHub Actions and GitLab CI. Case study from a Fortune 500 retail company that reduced critical CVEs by 73%.",
        "start_date": "2026-12-03",
        "start_time": "2:00 PM",
        "scheduled_start": "2026-12-03T14:00:00",
        "scheduled_end": "2026-12-03T15:00:00",
        "location": "MGM Grand | Level 1 | Grand Ballroom A",
        "location_mode": "physical",
        "learning_level": "Advanced",
        "event_type": "Breakout Session",
        "partner_name": "Snyk",
        "score": 0.83,
        "registration_url": "https://registration.awsevents.com/flow/awsevents/reinvent26/reg/login",
        "learn_more_url": "https://reinvent.awsevents.com/",
    },
]

MOCK_ALTERNATIVES = [
    {
        "event_id": "a001",
        "title": "Agentic AI Governance for Regulated Industries",
        "description": "Compliance frameworks, audit trails, and human-in-the-loop patterns for agentic systems under financial regulation.",
        "start_time": "1:00 PM",
        "location": "Caesars Forum | Forum 120",
        "learning_level": "Advanced",
        "session_type": "Breakout Session",
        "areas_of_interest": ["Agentic AI", "Security"],
        "score": 0.91,
        "registration_url": "https://registration.awsevents.com/flow/awsevents/reinvent26/reg/login",
    },
    {
        "event_id": "a002",
        "title": "Responsible AI at scale: guardrails, evals, and red-teaming",
        "description": "End-to-end safety framework for LLM-powered applications in production: automated red-teaming pipelines, Bedrock Guardrails tuning, and continuous eval with human review.",
        "start_time": "1:00 PM",
        "location": "Venetian | Level 4 | Murano 3202",
        "learning_level": "Expert",
        "session_type": "Chalk Talk",
        "areas_of_interest": ["Generative AI", "Security"],
        "score": 0.88,
        "registration_url": "https://registration.awsevents.com/flow/awsevents/reinvent26/reg/login",
    },
    {
        "event_id": "a003",
        "title": "LLM fine-tuning on SageMaker: from LoRA to full pre-training",
        "description": "Step-by-step guide to choosing the right fine-tuning strategy, managing training jobs on SageMaker HyperPod, and running cost-effective evals with Amazon Bedrock.",
        "start_time": "1:30 PM",
        "location": "Venetian | Level 3 | Lando 4201",
        "learning_level": "Expert",
        "session_type": "Breakout Session",
        "areas_of_interest": ["Machine Learning", "Generative AI"],
        "score": 0.84,
        "registration_url": "https://registration.awsevents.com/flow/awsevents/reinvent26/reg/login",
    },
]


async def screenshot(html_content: str, output_path: Path, width: int = 1280, full_page: bool = True) -> None:
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": width, "height": 900})
        await page.set_content(html_content, wait_until="networkidle")
        await page.screenshot(path=str(output_path), full_page=full_page)
        await browser.close()
    print(f"  ✓ {output_path.name}")


async def main():
    out = Path(__file__).parent.parent / "docs" / "screenshots"
    out.mkdir(parents=True, exist_ok=True)

    print("Generating screenshots…")

    # 1. Schedule view — dark mode, full day
    html = html_schedule(MOCK_SCHEDULE)
    html_dark = html.replace(
        "<html lang=\"en\">",
        "<html lang=\"en\" data-theme=\"dark\">"
    )
    await screenshot(html_dark, out / "schedule_dark.png", width=1200, full_page=True)

    # 2. Schedule view — light mode
    await screenshot(html, out / "schedule_light.png", width=1200, full_page=True)

    # 3. Details modal open — inject JS to open it automatically
    html_with_modal = html_dark.replace(
        "</body>",
        "<script>window.addEventListener('load',()=>{openDetails('evt002');});</script></body>"
    )
    await screenshot(html_with_modal, out / "detail_modal.png", width=1200, full_page=False)

    # 4. Schedule + edit panel open (serve mode)
    html_serve = html_schedule(MOCK_SCHEDULE, interactive_port=8080)
    html_serve_dark = html_serve.replace(
        "<html lang=\"en\">",
        "<html lang=\"en\" data-theme=\"dark\">"
    ).replace(
        "</body>",
        """<script>window.addEventListener('load', async () => {
          // Fake the API response for the screenshot
          const orig = window.fetch;
          window.fetch = async (url, opts) => {
            if (url.includes('/api/alternatives')) {
              return { ok: true, json: async () => """ + json.dumps(MOCK_ALTERNATIVES) + """ };
            }
            return orig(url, opts);
          };
          const btn = document.querySelector('#eb-evt002');
          if (btn) { await toggleEdit(btn, 'evt002', '2026-12-02T13:00'); }
        });</script></body>"""
    )
    await screenshot(html_serve_dark, out / "edit_panel.png", width=1200, full_page=False)

    print(f"\nDone → docs/screenshots/")
    for f in sorted(out.glob("*.png")):
        kb = f.stat().st_size // 1024
        print(f"  {f.name}  ({kb} KB)")


if __name__ == "__main__":
    asyncio.run(main())
