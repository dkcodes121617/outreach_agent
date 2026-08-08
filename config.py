"""Configuration for the Outreach agent.

This is the only agent in the system that can do something to a stranger that
cannot be undone, so its validation is the strictest: the legal prerequisites
for cold email are checked as hard requirements the moment `email` appears in
`CHANNELS_ENABLED`, not as warnings.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from wizcore.config import ConfigError, env_bool, env_float, env_int, env_list, env_str, load_env

AGENT_ROOT = Path(__file__).resolve().parent
load_env(AGENT_ROOT)

AGENT_NAME = "outreach"

ALL_CHANNELS = ("email", "linkedin", "whatsapp")


@dataclass(frozen=True)
class Config:
    dry_run: bool = env_bool("DRY_RUN", True)
    log_level: str = env_str("LOG_LEVEL", "INFO")
    display_tz: str = env_str("DISPLAY_TZ", "Asia/Kolkata")

    database_url: str = env_str("NEON_DATABASE_URL")
    checkpoint_schema: str = env_str("LANGGRAPH_CHECKPOINT_SCHEMA", "oa_ckpt")

    site_repo: str = env_str("SITE_REPO", "dkcodes121617/wizcodes_main_website")
    site_branch: str = env_str("SITE_REPO_BRANCH", "main")
    site_read_token: str = env_str("SITE_READ_TOKEN")
    site_local_dir: str = env_str("SITE_LOCAL_DIR")

    channels_enabled: list[str] = field(
        default_factory=lambda: env_list("CHANNELS_ENABLED", "linkedin,whatsapp")
    )

    # ── claiming ──
    claim_batch: int = env_int("CLAIM_BATCH", 10)
    claim_lease_minutes: int = env_int("CLAIM_LEASE_MINUTES", 30)
    retouch_cooldown_days: int = env_int("RETOUCH_COOLDOWN_DAYS", 90)

    # ── assessment ──
    pagespeed_key: str = env_str("GOOGLE_PAGESPEED_API_KEY")
    pagespeed_strategy: str = env_str("PAGESPEED_STRATEGY", "mobile")
    min_weakness_score: int = env_int("MIN_WEAKNESS_SCORE", 45)
    assessment_ttl_days: int = env_int("ASSESSMENT_TTL_DAYS", 45)
    groq_api_key: str = env_str("GROQ_API_KEY")
    # Blank by default, and deliberately so — see assess/vision.py. No
    # multimodal model is available on this Groq account, and the Claude proxy
    # silently strips image blocks. A non-empty default here would make every
    # ambiguous prospect issue a doomed 404.
    vision_model: str = env_str("VISION_MODEL", "")
    vision_only_when_ambiguous: bool = env_bool("VISION_ONLY_WHEN_AMBIGUOUS", True)

    # ── enrichment ──
    require_mx_check: bool = env_bool("REQUIRE_MX_CHECK", True)
    email_patterns: list[str] = field(
        default_factory=lambda: env_list("EMAIL_PATTERNS", "info,contact,hello")
    )
    tavily_api_key: str = env_str("TAVILY_API_KEY")

    # ── sending ──
    email_provider: str = env_str("EMAIL_PROVIDER", "brevo").lower()
    brevo_api_key: str = env_str("BREVO_API_KEY")
    from_email: str = env_str("OUTREACH_FROM_EMAIL")
    from_name: str = env_str("OUTREACH_FROM_NAME", "WizCodes")
    reply_to: str = env_str("OUTREACH_REPLY_TO")
    postal_address: str = env_str("OUTREACH_POSTAL_ADDRESS")
    unsubscribe_base_url: str = env_str("UNSUBSCRIBE_BASE_URL")
    unsubscribe_secret: str = env_str("UNSUBSCRIBE_SECRET")

    max_emails_per_day: int = env_int("MAX_EMAILS_PER_DAY", 20)
    max_emails_per_run: int = env_int("MAX_EMAILS_PER_RUN", 10)
    send_jitter_seconds: int = env_int("SEND_JITTER_SECONDS", 180)
    followup_after_days: int = env_int("FOLLOWUP_AFTER_DAYS", 6)
    # Two touches, never five. From the recipient's side, the difference between
    # outreach and spam is mostly the number of times you email someone who did
    # not reply.
    max_sequence_steps: int = env_int("MAX_SEQUENCE_STEPS", 2)

    # ── guard rails (EMAIL_PLAYBOOK §4) ──
    max_bounce_rate: float = env_float("MAX_BOUNCE_RATE", 0.03)
    max_complaint_rate: float = env_float("MAX_COMPLAINT_RATE", 0.001)

    # ── LLM ──
    voice_model: str = env_str("ANTHROPIC_VOICE_MODEL", "claude-opus-4-8")

    budget_caps: dict[str, float] = field(
        default_factory=lambda: {
            "claude_proxy": env_int("BUDGET_CLAUDE_KTOKENS", 300),
            "groq": env_int("BUDGET_GROQ_CALLS", 200),
            "pagespeed": env_int("BUDGET_PSI_CALLS", 200),
            "brevo": env_int("MAX_EMAILS_PER_DAY", 20),
        }
    )
    http_timeout: int = env_int("HTTP_TIMEOUT", 30)

    def active_channels(self) -> list[str]:
        return [c for c in self.channels_enabled if c in ALL_CHANNELS]

    def email_enabled(self) -> bool:
        return "email" in self.active_channels()

    def validate(self) -> None:
        problems: list[str] = []
        if not self.database_url:
            problems.append("NEON_DATABASE_URL is not set")
        unknown = [c for c in self.channels_enabled if c not in ALL_CHANNELS]
        if unknown:
            problems.append(
                f"CHANNELS_ENABLED names unknown channel(s): {', '.join(unknown)}. "
                f"Known: {', '.join(ALL_CHANNELS)}"
            )
        if not self.active_channels():
            problems.append("CHANNELS_ENABLED is empty - the run would draft nothing")
        if not self.pagespeed_key:
            problems.append("GOOGLE_PAGESPEED_API_KEY is not set; assessment cannot run")
        if not self.site_read_token and not self.site_local_dir:
            problems.append(
                "SITE_READ_TOKEN is not set and SITE_LOCAL_DIR is empty - grounding "
                "facts cannot be built, and an email citing an invented project is "
                "worse than no email"
            )

        # ── the legal gate ──
        # Checked only when email is actually on, so the agent can do assessment
        # and drafting work long before any of this is arranged. The moment it
        # IS on, these stop being advice: CAN-SPAM requires a working opt-out
        # and a physical postal address in every commercial email, and a missing
        # unsubscribe is the fastest route to the complaint rate that ends a
        # sending domain.
        if self.email_enabled():
            if not self.postal_address:
                problems.append(
                    "OUTREACH_POSTAL_ADDRESS is required by CAN-SPAM in every "
                    "commercial email"
                )
            if not self.unsubscribe_base_url or not self.unsubscribe_secret:
                problems.append(
                    "UNSUBSCRIBE_BASE_URL and UNSUBSCRIBE_SECRET are both required "
                    "before any cold email - an opt-out nobody can find becomes a "
                    "spam complaint"
                )
            if not self.from_email:
                problems.append("OUTREACH_FROM_EMAIL is not set")
            if not self.reply_to:
                problems.append(
                    "OUTREACH_REPLY_TO is not set. mail.wizcodes.site is a CNAME to "
                    "Brevo whose MX is a bounce handler, so replies sent to the From "
                    "address would vanish into bounce processing"
                )
            if self.email_provider == "brevo" and not self.brevo_api_key:
                problems.append("EMAIL_PROVIDER=brevo but BREVO_API_KEY is not set")
            if self.email_provider == "ses" and not (
                env_str("AWS_ACCESS_KEY_ID") and env_str("AWS_SECRET_ACCESS_KEY")
            ):
                problems.append(
                    "EMAIL_PROVIDER=ses but AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY "
                    "are empty. Everything else here is configured for Brevo "
                    "(BREVO_API_KEY is set, mail.wizcodes.site is a verified Brevo "
                    "sending domain) - set EMAIL_PROVIDER=brevo"
                )
            if self.email_provider not in ("brevo", "ses"):
                problems.append(f"unknown EMAIL_PROVIDER {self.email_provider!r}")

        if problems:
            raise ConfigError(
                "outreach configuration is not runnable:\n  - " + "\n  - ".join(problems)
            )


CONFIG = Config()
