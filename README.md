# MSD Flows

Version-controlled repository for Matcha Design Studio / Langflow workflows used for CEQ auditing, call analysis, ticket evaluation, knowledge management, and operational automation.

## Repository Structure

```text
MSD Flows
│
├── CEQ Audit/
│   ├── CALL AUDITS.json
│   ├── CALL DISCONNECT.json
│   ├── CEQ DISPATCHER.json
│   └── EMAIL AUDITS.json
│
├── My Flows/
│   ├── Chatbot.json
│   ├── Diagnostics.json
│   ├── KB Source Code.json
│   ├── MCH FLOW.json
│   └── SNOW-Backlog-Analyzer.json
│
├── Inactive flows/
│   ├── CALL AUDITS TEST.json
│   ├── CEQ Analytics.json
│   ├── CEQ Pilot Evaluation.json
│   ├── Call QA Auditor.json
│   └── ...
│
├── docs/
│   ├── Architecture Overview.md
│   ├── CEQ AI Call Audit Platform_ Leadership Briefing.pdf
│   └── CEQ CALL AUDITS_ Process Flow v1.2 1.pdf
│
└── README.md


Primary Flows
CALL AUDITS

Core CEQ audit workflow responsible for:

Call retrieval and processing
Transcript analysis
Audit scoring
Compliance evaluation
Opportunity identification
CEQ DISPATCHER

Central orchestration layer responsible for:

Request routing
Workflow selection
Agent and queue mapping
Audit distribution
CALL DISCONNECT

Focused workflow for:

Disconnect reason analysis
Call termination investigation
Agent/user disconnect validation
EMAIL AUDITS

Email-driven audit intake workflow responsible for:

Audit request handling
Notification processing
Audit tracking support
Supporting Flows
SNOW-Backlog-Analyzer

ServiceNow backlog analysis and ticket evaluation.

KB Source Code

Knowledge Base retrieval and source validation.

Diagnostics

Flow testing and troubleshooting utilities.

Chatbot

General-purpose conversational testing flow.

Objectives
Improve audit consistency
Reduce manual review effort
Standardize quality evaluations
Support coaching and corrective actions
Provide actionable operational insights


Security

Do not commit:

API Keys
OAuth Secrets
ServiceNow Credentials
RingCentral Tokens
Bearer Tokens
Passwords

Repository should remain private.

Owner

Faiz Sayyed

Associate Service Desk Analyst Altera Digital Health
