# Security Policy

## Supported Versions

| Version | Supported |
|---|---|
| latest | ✅ |
| < latest | ❌ — please upgrade |

## Reporting a Vulnerability

**Do NOT open a public GitHub issue for security vulnerabilities.**

If you find a security vulnerability in Synaptic, please report it privately:

**Email: amit@zistica.com**
**Subject: [SECURITY] Brief description**

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Your suggested fix (if any)

### What happens next

- You will receive acknowledgment within 48 hours
- We will investigate and keep you updated
- We will notify you when a fix is released
- We will credit you in the release notes (unless you prefer anonymity)

### Scope

In scope:
- Python SDK sending data to unintended destinations
- API endpoints leaking data across projects
- Docker image with known CVEs
- Dependency vulnerabilities

Out of scope:
- Vulnerabilities in the developer's own infrastructure
- Social engineering attacks
- Denial of service via normal usage

## Our Commitment

Synaptic is local-first. By design, your agent data never leaves your machine in the default configuration. If you find any behavior that contradicts this — please report it immediately. This is the most critical security property of Synaptic.
