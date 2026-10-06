from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FRAUDALERT_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://fraudalert:fraudalert@localhost:5432/fraudalert"

    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    sender_filter: str = ""
    subject_filter: str = ""
    lookback_days: int = 90
    # Monthly bank statements (PDF attachments): who sends them, e.g. "estadodecuenta@baccredomatic.cr",
    # optional subject keywords, and the password if your bank encrypts the PDFs. Empty sender = off.
    statement_sender_filter: str = ""
    statement_subject_filter: str = ""
    statement_password: str = ""
    statement_sync_hours: int = 24  # statements arrive monthly: check for them at most this often
    # One-time-password emails ("your verification code is ..."). An OTP you didn't ask for means someone is
    # using your card details right now, so every OTP request raises a Critical (P0) alarm until you acknowledge
    # it. Identify them by subject keywords and/or sender (both must match when both are set); senders listed
    # here are fetched even if FRAUDALERT_SENDER_FILTER doesn't include them. Both empty = off.
    otp_subject_filter: str = ""
    otp_sender_filter: str = ""

    # Outgoing mail for the daily report. Blank user/password = reuse the IMAP login (works for Gmail
    # app passwords). Port 587 = STARTTLS, 465 = SSL.
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""

    home_currency: str = "USD"  # rules and anomaly features compare amounts in this currency
    # Currencies you normally pay in, e.g. "CRC,USD"; any other currency counts as foreign.
    # Defaults to the home currency. Editable on the web UI's Settings page (which takes precedence).
    normal_currencies: str = ""
    home_country: str = ""  # e.g. "Costa Rica"; when the email names a country, others count as foreign
    fx_rates: str = ""  # overrides for fraudalert/fx.py, e.g. "CRC=0.00195" (1 CRC in home currency)
    # Card-test detection: an authorisation at or below this amount (home currency) is a likely card
    # test, and a larger charge on the same card within `test_followup_hours` is escalated.
    test_amount_max: float = 1.0
    test_followup_hours: int = 48
    timezone: str = "America/New_York"  # your local time: email dates, hour-of-day rules and features
    detector: str = "iforest"  # "iforest" (falls back to "baseline" until trained) or "baseline"
    notify_webhook_url: str = ""

    web_bind: str = "127.0.0.1"  # host interface docker compose publishes the UI on
    web_username: str = ""
    web_password: str = ""

    @staticmethod
    def _split(value: str) -> list[str]:
        return [v.strip() for v in value.split(",") if v.strip()]

    @property
    def auth_enabled(self) -> bool:
        return bool(self.web_username and self.web_password)

    @property
    def senders(self) -> list[str]:
        return self._split(self.sender_filter)

    @property
    def subjects(self) -> list[str]:
        return self._split(self.subject_filter)

    @property
    def statement_senders(self) -> list[str]:
        return self._split(self.statement_sender_filter)

    @property
    def otp_subjects(self) -> list[str]:
        return self._split(self.otp_subject_filter)

    @property
    def otp_senders(self) -> list[str]:
        return self._split(self.otp_sender_filter)

    @property
    def otp_enabled(self) -> bool:
        return bool(self.otp_subjects or self.otp_senders)

    def is_otp(self, sender: str, subject: str) -> bool:
        """Is this email an OTP request? Case-insensitive substring matches, like the other filters."""
        if not self.otp_enabled:
            return False
        sender, subject = (sender or "").lower(), (subject or "").lower()
        return ((not self.otp_senders or any(s.lower() in sender for s in self.otp_senders))
                and (not self.otp_subjects or any(s.lower() in subject for s in self.otp_subjects)))

    def for_statements(self) -> "Settings":
        """The same mailbox, searched for statement emails instead of transaction alerts."""
        return self.model_copy(update={"sender_filter": self.statement_sender_filter,
                                       "subject_filter": self.statement_subject_filter,
                                       "statement_sender_filter": "",
                                       "otp_subject_filter": "", "otp_sender_filter": ""})  # nothing to exclude in this search


@lru_cache
def get_settings() -> Settings:
    return Settings()
