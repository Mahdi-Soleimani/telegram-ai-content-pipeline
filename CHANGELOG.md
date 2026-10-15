# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-10-15

### Added
- **Asynchronous Relay Userbot**:
  - Telethon-powered non-blocking listener for source Telegram channels.
  - Persistent SQLite message pointer queue with Write-Ahead Logging (WAL) and dead-letter retry logic.
  - Just-In-Time (JIT) live message retrieval discarding deleted upstream posts.
  - Album settlement buffer ensuring complete multi-part media dispatch.
  - Telegram FloodWait resilience isolating dispatcher pauses from ingestion.
  - Anti-ban jitter randomization and bounded delivery retry policies.
  - Integrated HTTP liveness responder for cloud orchestrator healthchecks.
- **Web-Based Session Generator**:
  - Sleek Persian RTL web interface for headless PaaS authentication.
  - 3-step wizard supporting phone login, Telegram OTP, and 2FA Cloud Passwords.
  - Thread-safe Waitress WSGI integration with session memory hygiene.
- **n8n Automation Engine & Multi-Model AI Pipeline**:
  - Dual-model LLM architecture using Google Gemini 2.5 Flash with DeepSeek V4 Flash fallback.
  - Specialized journalistic prompts for high-density Persian financial news synthesis.
  - Market overview numerical extractor for crypto signals and metrics.
  - Comprehensive media support (photos, videos, audio, voice notes, documents, animations, and albums).
  - Human-in-the-Loop review system with interactive Telegram inline buttons (Approve, Reject, Save).
  - Automated database archiving and scheduled market digest broadcaster.
  - AI error diagnosis sub-workflow providing one-sentence Persian troubleshooting alerts.
- **DevOps & Containerization**:
  - Multi-stage Dockerfiles with unprivileged non-root users.
  - Root `docker-compose.yml` orchestrating userbot, session generator, and n8n with volume persistence.
  - Automated CI workflow for linting, syntax checking, and secret auditing.
