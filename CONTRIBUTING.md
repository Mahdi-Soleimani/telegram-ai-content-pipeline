# Contributing Guidelines

Thank you for your interest in contributing to **Telegram AI Content Pipeline**! We welcome bug reports, feature proposals, documentation improvements, and pull requests.

## Code of Conduct

This project adheres to the Contributor Covenant [Code of Conduct](CODE_OF_CONDUCT.md). By participating, you are expected to uphold this code.

## Getting Started

1. **Fork the Repository**: Create your own fork of [Mahdi-Soleimani/telegram-ai-content-pipeline](https://github.com/Mahdi-Soleimani/telegram-ai-content-pipeline).
2. **Clone Locally**:
   ```bash
   git clone https://github.com/<your-username>/telegram-ai-content-pipeline.git
   cd telegram-ai-content-pipeline
   ```
3. **Create a Feature Branch**:
   ```bash
   git checkout -b feat/your-feature-name
   ```

## Development Standards

- **Python**: Follow PEP 8 style guidelines. Type hints are strongly encouraged.
- **Workflow Definitions**: Any proposed n8n workflow additions must be placed in `n8n-bot/workflows/` and strictly sanitized:
  - NEVER commit actual bot tokens, private channel IDs, API keys, or webhook IDs.
  - Use generic placeholders or dynamic `$env` parameters.
- **Asynchronous Design**: In `account-controler`, maintain non-blocking asyncio patterns and ensure database connections utilize WAL mode and transactional safety.

## Commit Message Conventions

We follow [Conventional Commits](https://www.conventionalcommits.org/):
- `feat: add support for dynamic album retry`
- `fix: resolve flood wait duration calculation`
- `docs: update deployment instructions for Caddy`
- `chore: update dependencies`
- `ci: add automated linting workflow`

## Pull Request Process

1. Ensure all code passes syntax and security checks (`gitleaks dir .`).
2. Update relevant documentation and `.env.example` if adding new configuration variables.
3. Submit a pull request to the `main` branch with a clear summary of changes and testing instructions.
