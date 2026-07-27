# Low-Level Design (LLD)

## Overview

This folder contains the detailed technical design for the **MBB ya Kin** WhatsApp chatbot system. Each document focuses on one implementation domain, making the design easier to review, maintain, and implement.

> **Current-status boundary:** These documents preserve April 2026 target-state
> specifications and historical implementation guidance. They are not current
> runtime or readiness evidence. In particular, legacy `+243`-only phone rules
> are superseded by the application's canonical international E.164 validation.

The LLD builds on the High-Level Design and provides implementation-ready specifications for backend services, data structures, API contracts, security controls, and operational behavior.

## Files In This Folder

### 1. Module Component Design.md

Describes the internal modules, their responsibilities, inputs and outputs, dependencies, and interaction patterns.

### 2. Database Design.md

Defines the PostgreSQL schema, Redis structures, materialized views, indexing strategy, and data integrity rules.

### 3. API Detailed Specification.md

Documents the FastAPI endpoints, request and response schemas, validation rules, status codes, and DTO models.

### 4. Security Design.md

Explains authentication, authorization, encryption, rate limiting, infrastructure protection, and privacy controls.

### 5. Exception & Error Handling.md

Covers error classification, standard error codes, retry logic, circuit breakers, graceful degradation, logging, and monitoring.

## Purpose

The goal of this folder is to provide implementation-ready design details for the backend, orchestration, data, API, security, and operational behavior of the system.

## Recommended Reading Order

1. Module Component Design
2. Database Design
3. API Detailed Specification
4. Security Design
5. Exception & Error Handling

## Design Principles

All designs in this folder follow these principles:
- **Layered Architecture**: Services organized across 5 logical layers (Interaction → Orchestration → Business Logic → Data → Infrastructure)
- **Separation of Concerns**: Each module has a single, well-defined responsibility
- **Technology Agnostic Details**: Implementation-specific details included for FastAPI, PostgreSQL, Redis, Celery
- **Error Resilience**: Circuit breakers, retry logic, and graceful degradation built into every layer
- **DRC Infrastructure Constraints**: Designs account for 3G bandwidth, power instability, and UTC+1 timezone requirements

## Using This Documentation

- **For Implementation**: Start with Module Component Design, then reference Database Design and API Specification
- **For System Integration**: Use API Detailed Specification as the contract between Celery tasks and FastAPI
- **For Operations**: Review Security Design and Exception & Error Handling for deployment and troubleshooting
- **For Review**: Reading order is recommended top-to-bottom

## Cross-References

- **High-Level Design**: See `../High Level Design/` for architecture overview, technology stack, and design decisions
- **Functional Requirements**: See `../../functional-and-non-functional-requirements.md` for system capabilities and constraints
