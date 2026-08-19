"""Forward-only, checksummed SQLite schema migrations."""

import hashlib
from dataclasses import dataclass


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Migration:
    version: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        content = "\n-- statement --\n".join(statement.strip() for statement in self.statements)
        return hashlib.sha256(content.encode("utf-8")).hexdigest()


_INITIAL_SCHEMA = Migration(
    "001_initial_facts",
    (
        """
        CREATE TABLE inbox_message (
            consumer_id TEXT NOT NULL,
            msg_id TEXT NOT NULL,
            dedup_key TEXT,
            status TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
            received_at TEXT NOT NULL,
            processed_at TEXT,
            error_id TEXT,
            correlation_id TEXT NOT NULL,
            PRIMARY KEY (consumer_id, msg_id)
        )
        """,
        """
        CREATE TABLE outbox_event (
            event_id TEXT PRIMARY KEY,
            msg_type TEXT NOT NULL,
            envelope_json TEXT NOT NULL CHECK (length(envelope_json) <= 65536),
            publish_state TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
            next_attempt_at TEXT,
            created_at TEXT NOT NULL,
            published_at TEXT,
            correlation_id TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_outbox_pending ON outbox_event(publish_state, next_attempt_at)",
        """
        CREATE TABLE dead_letter (
            dead_letter_id TEXT PRIMARY KEY,
            consumer_id TEXT NOT NULL,
            msg_id TEXT NOT NULL,
            envelope_json TEXT NOT NULL CHECK (length(envelope_json) <= 65536),
            error_id TEXT NOT NULL,
            failed_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE plan (
            plan_id TEXT PRIMARY KEY,
            plan_json TEXT NOT NULL,
            digest TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_plan_status_expiry ON plan(status, expires_at)",
        """
        CREATE TABLE plan_decision (
            decision_id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL REFERENCES plan(plan_id),
            decision TEXT NOT NULL,
            decision_json TEXT NOT NULL,
            decided_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            UNIQUE (plan_id)
        )
        """,
        """
        CREATE TABLE execution_grant (
            grant_id TEXT PRIMARY KEY,
            decision_id TEXT NOT NULL REFERENCES plan_decision(decision_id),
            task_id TEXT NOT NULL UNIQUE,
            grant_json TEXT NOT NULL,
            status TEXT NOT NULL,
            issued_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE task (
            task_id TEXT PRIMARY KEY,
            grant_id TEXT NOT NULL UNIQUE REFERENCES execution_grant(grant_id),
            status TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
            attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            deadline TEXT NOT NULL,
            error_id TEXT,
            correlation_id TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_task_status_deadline ON task(status, deadline)",
        """
        CREATE TABLE task_transition (
            transition_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES task(task_id),
            from_status TEXT,
            to_status TEXT NOT NULL,
            reason TEXT NOT NULL,
            attempt INTEGER NOT NULL CHECK (attempt >= 0),
            event_id TEXT NOT NULL UNIQUE,
            occurred_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_task_transition_task ON task_transition(task_id, occurred_at)",
        """
        CREATE TABLE workflow_run (
            run_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES task(task_id),
            workflow_id TEXT NOT NULL,
            workflow_version TEXT NOT NULL,
            workflow_digest TEXT NOT NULL,
            input_digest TEXT NOT NULL,
            status TEXT NOT NULL,
            parent_run_id TEXT REFERENCES workflow_run(run_id),
            deadline TEXT NOT NULL,
            created_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE node_run (
            run_id TEXT NOT NULL REFERENCES workflow_run(run_id),
            node_id TEXT NOT NULL,
            attempt INTEGER NOT NULL CHECK (attempt >= 1),
            skill_binding_id TEXT,
            status TEXT NOT NULL,
            input_artifact_id TEXT,
            output_artifact_id TEXT,
            error_id TEXT,
            usage_json TEXT,
            started_at TEXT,
            finished_at TEXT,
            correlation_id TEXT NOT NULL,
            PRIMARY KEY (run_id, node_id, attempt)
        )
        """,
        """
        CREATE TABLE skill_binding (
            binding_id TEXT PRIMARY KEY,
            grant_id TEXT NOT NULL REFERENCES execution_grant(grant_id),
            capability TEXT NOT NULL,
            skill_id TEXT NOT NULL,
            skill_version TEXT NOT NULL,
            skill_digest TEXT NOT NULL,
            binding_json TEXT NOT NULL,
            resolved_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            UNIQUE (grant_id, capability)
        )
        """,
        """
        CREATE TABLE episode (
            episode_id TEXT PRIMARY KEY,
            task_id TEXT REFERENCES task(task_id),
            episode_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE outcome_evaluation (
            evaluation_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES task(task_id),
            episode_id TEXT REFERENCES episode(episode_id),
            evaluation_json TEXT NOT NULL,
            evaluated_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE memory_entry (
            memory_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            content_json TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            valid_until TEXT,
            created_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_memory_kind_validity ON memory_entry(kind, valid_until)",
        """
        CREATE TABLE workflow_definition (
            workflow_id TEXT NOT NULL,
            version TEXT NOT NULL,
            digest TEXT NOT NULL,
            status TEXT NOT NULL,
            definition_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            PRIMARY KEY (workflow_id, version),
            UNIQUE (workflow_id, digest)
        )
        """,
        """
        CREATE UNIQUE INDEX idx_workflow_one_active
        ON workflow_definition(workflow_id) WHERE status = 'ACTIVE'
        """,
        """
        CREATE TABLE skill_manifest (
            skill_id TEXT NOT NULL,
            version TEXT NOT NULL,
            digest TEXT NOT NULL,
            status TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            PRIMARY KEY (skill_id, version),
            UNIQUE (skill_id, digest)
        )
        """,
        """
        CREATE TABLE capability_contract (
            capability TEXT NOT NULL,
            version TEXT NOT NULL,
            digest TEXT NOT NULL,
            status TEXT NOT NULL,
            contract_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            PRIMARY KEY (capability, version),
            UNIQUE (capability, digest)
        )
        """,
        """
        CREATE TABLE evolution_lineage (
            evolution_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            base_digest TEXT NOT NULL,
            candidate_digest TEXT NOT NULL,
            patch_json TEXT NOT NULL,
            hypothesis TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            decision TEXT NOT NULL,
            created_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE artifact (
            artifact_id TEXT PRIMARY KEY,
            uri TEXT NOT NULL,
            digest TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
            media_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            UNIQUE (uri, digest)
        )
        """,
        """
        CREATE TABLE audit_record (
            audit_id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            subject_type TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            previous_audit_id TEXT REFERENCES audit_record(audit_id),
            record_json TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_audit_subject ON audit_record(subject_type, subject_id, occurred_at)",
    ),
)


_INBOX_BUSINESS_DEDUP = Migration(
    "002_inbox_business_dedup",
    (
        """
        CREATE UNIQUE INDEX idx_inbox_consumer_dedup_key
        ON inbox_message(consumer_id, dedup_key)
        WHERE dedup_key IS NOT NULL
        """,
        "CREATE INDEX idx_inbox_status ON inbox_message(consumer_id, status)",
        "CREATE INDEX idx_dead_letter_message ON dead_letter(consumer_id, msg_id)",
    ),
)


_SCHEDULER_CHECKPOINT = Migration(
    "003_scheduler_checkpoint",
    (
        """
        CREATE TABLE schedule_checkpoint (
            schedule_id TEXT NOT NULL,
            occurrence_key TEXT NOT NULL,
            status TEXT NOT NULL,
            fired_at TEXT,
            consumed_at TEXT NOT NULL,
            PRIMARY KEY (schedule_id, occurrence_key)
        )
        """,
        "CREATE INDEX idx_schedule_checkpoint_status ON schedule_checkpoint(status, consumed_at)",
    ),
)


_WORKFLOW_RUN_TRANSITIONS = Migration(
    "004_workflow_run_transitions",
    (
        "ALTER TABLE workflow_run ADD COLUMN version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0)",
        "ALTER TABLE workflow_run ADD COLUMN started_at TEXT",
        "ALTER TABLE workflow_run ADD COLUMN finished_at TEXT",
        "ALTER TABLE workflow_run ADD COLUMN error_id TEXT",
        "ALTER TABLE node_run ADD COLUMN version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0)",
        "ALTER TABLE node_run ADD COLUMN created_at TEXT",
        """
        CREATE TABLE workflow_run_transition (
            transition_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES workflow_run(run_id),
            from_status TEXT,
            to_status TEXT NOT NULL,
            reason TEXT NOT NULL,
            version INTEGER NOT NULL CHECK (version >= 0),
            event_id TEXT NOT NULL UNIQUE,
            occurred_at TEXT NOT NULL,
            error_id TEXT,
            correlation_id TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_workflow_transition_run ON workflow_run_transition(run_id, version)",
        """
        CREATE TABLE node_run_transition (
            transition_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            attempt INTEGER NOT NULL CHECK (attempt >= 1),
            from_status TEXT,
            to_status TEXT NOT NULL,
            reason TEXT NOT NULL,
            version INTEGER NOT NULL CHECK (version >= 0),
            event_id TEXT NOT NULL UNIQUE,
            occurred_at TEXT NOT NULL,
            error_id TEXT,
            correlation_id TEXT NOT NULL,
            FOREIGN KEY (run_id, node_id, attempt)
                REFERENCES node_run(run_id, node_id, attempt)
        )
        """,
        "CREATE INDEX idx_node_transition_run ON node_run_transition(run_id, node_id, attempt, version)",
        "CREATE INDEX idx_workflow_run_status ON workflow_run(status, deadline)",
        "CREATE INDEX idx_node_run_status ON node_run(status, run_id)",
    ),
)


_NODE_INLINE_OUTPUT = Migration(
    "005_node_inline_output",
    (
        "ALTER TABLE node_run ADD COLUMN output_json TEXT CHECK (output_json IS NULL OR length(output_json) <= 1048576)",
    ),
)


_PLAN_GRANT_LIFECYCLE = Migration(
    "006_plan_grant_lifecycle",
    (
        "CREATE UNIQUE INDEX idx_plan_digest ON plan(digest)",
        "CREATE UNIQUE INDEX idx_grant_decision_task ON execution_grant(decision_id, task_id)",
        """
        CREATE TABLE execution_grant_transition (
            transition_id TEXT PRIMARY KEY,
            grant_id TEXT NOT NULL REFERENCES execution_grant(grant_id),
            from_status TEXT,
            to_status TEXT NOT NULL,
            reason TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_grant_transition ON execution_grant_transition(grant_id, occurred_at)",
        """
        CREATE TABLE grant_attempt (
            grant_id TEXT NOT NULL REFERENCES execution_grant(grant_id),
            task_id TEXT NOT NULL,
            attempt INTEGER NOT NULL CHECK (attempt >= 1),
            authorized_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            PRIMARY KEY (grant_id, attempt),
            UNIQUE (task_id, attempt)
        )
        """,
    ),
)


_DELAYED_OUTCOME_LEDGER = Migration(
    "007_delayed_outcome_ledger",
    (
        """
        CREATE TABLE delayed_evaluation_window (
            window_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES task(task_id),
            episode_id TEXT REFERENCES episode(episode_id),
            opens_at TEXT NOT NULL,
            closes_at TEXT NOT NULL,
            status TEXT NOT NULL,
            evaluator_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            CHECK (closes_at > opens_at)
        )
        """,
        "CREATE INDEX idx_delayed_window_due ON delayed_evaluation_window(status, closes_at)",
        """
        CREATE TABLE evidence_ledger (
            ledger_id TEXT PRIMARY KEY,
            window_id TEXT NOT NULL REFERENCES delayed_evaluation_window(window_id),
            evidence_id TEXT NOT NULL,
            evidence_type TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            evidence_digest TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            UNIQUE (window_id, evidence_id)
        )
        """,
        "CREATE INDEX idx_evidence_ledger_window ON evidence_ledger(window_id, observed_at)",
    ),
)


_REST_REPAIR_RUN = Migration(
    "008_rest_repair_run",
    (
        """
        CREATE TABLE rest_repair_run (
            run_id TEXT PRIMARY KEY,
            review_key TEXT NOT NULL UNIQUE,
            business_date TEXT NOT NULL,
            status TEXT NOT NULL,
            attempt INTEGER NOT NULL CHECK (attempt >= 1),
            workflow_id TEXT NOT NULL,
            workflow_version TEXT NOT NULL,
            request_json TEXT NOT NULL,
            result_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deadline TEXT NOT NULL,
            correlation_id TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_rest_repair_status_deadline ON rest_repair_run(status, deadline)",
    ),
)


_SEMANTIC_MEMORY_CANDIDATES = Migration(
    "009_semantic_memory_candidates",
    (
        """
        CREATE TABLE semantic_memory (
            memory_id TEXT PRIMARY KEY,
            claim_key TEXT NOT NULL,
            claim_value_json TEXT NOT NULL,
            statement TEXT NOT NULL,
            summary TEXT NOT NULL,
            scope_json TEXT NOT NULL,
            scope_digest TEXT NOT NULL,
            conditions_json TEXT NOT NULL,
            confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
            validation_method TEXT,
            data_version TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            valid_until TEXT NOT NULL,
            status TEXT NOT NULL,
            contradicted_by_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            UNIQUE (claim_key, scope_digest, claim_value_json, data_version)
        )
        """,
        "CREATE INDEX idx_semantic_memory_status_validity ON semantic_memory(status, valid_until)",
        "CREATE INDEX idx_semantic_memory_claim_scope ON semantic_memory(claim_key, scope_digest)",
    ),
)


_LOCAL_NOTIFICATION_DELIVERY = Migration(
    "010_local_notification_delivery",
    (
        """
        CREATE TABLE local_notification_delivery (
            notification_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            payload_digest TEXT NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            level TEXT NOT NULL,
            result_json TEXT NOT NULL,
            delivered_at TEXT NOT NULL,
            task_id TEXT NOT NULL,
            run_id TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_local_notification_delivered ON local_notification_delivery(delivered_at, notification_id)",
    ),
)

_INSIGHT_DELIVERY_PREFERENCES = Migration(
    "011_insight_delivery_preferences",
    (
        """
        CREATE TABLE insight_subscription (
            subscription_id TEXT PRIMARY KEY,
            topic TEXT NOT NULL,
            minimum_level TEXT NOT NULL,
            channel TEXT NOT NULL,
            quiet_start_hour INTEGER CHECK (quiet_start_hour IS NULL OR quiet_start_hour BETWEEN 0 AND 23),
            quiet_end_hour INTEGER CHECK (quiet_end_hour IS NULL OR quiet_end_hour BETWEEN 0 AND 23),
            hourly_limit INTEGER NOT NULL CHECK (hourly_limit >= 1),
            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE insight_delivery (
            delivery_id TEXT PRIMARY KEY,
            subscription_id TEXT NOT NULL REFERENCES insight_subscription(subscription_id),
            insight_id TEXT NOT NULL,
            channel TEXT NOT NULL,
            delivered_at TEXT NOT NULL,
            read_at TEXT,
            UNIQUE(subscription_id, insight_id, channel)
        )
        """,
        "CREATE INDEX idx_insight_delivery_rate ON insight_delivery(subscription_id, delivered_at)",
    ),
)


_DNA_REGISTRY = Migration(
    "012_dna_registry",
    (
        """
        CREATE TABLE dna_definition (
            dna_id TEXT NOT NULL,
            version TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind = 'WORKFLOW'),
            status TEXT NOT NULL CHECK (status IN (
                'CANDIDATE','VALIDATED','SHADOW','CANARY','ACTIVE','DEPRECATED','RETIRED'
            )),
            content_digest TEXT NOT NULL,
            envelope_digest TEXT NOT NULL,
            document_json TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            PRIMARY KEY (dna_id, version),
            UNIQUE (dna_id, content_digest)
        )
        """,
        """
        CREATE UNIQUE INDEX idx_dna_one_active
        ON dna_definition(dna_id) WHERE status = 'ACTIVE'
        """,
        """
        CREATE TABLE dna_parent (
            child_dna_id TEXT NOT NULL,
            child_version TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            parent_dna_id TEXT NOT NULL,
            parent_version TEXT NOT NULL,
            parent_content_digest TEXT NOT NULL,
            PRIMARY KEY (child_dna_id, child_version, ordinal),
            UNIQUE (child_dna_id, child_version, parent_dna_id, parent_version),
            FOREIGN KEY (child_dna_id, child_version)
                REFERENCES dna_definition(dna_id, version),
            FOREIGN KEY (parent_dna_id, parent_version)
                REFERENCES dna_definition(dna_id, version)
        )
        """,
        """
        CREATE TABLE dna_transition (
            transition_id TEXT PRIMARY KEY,
            dna_id TEXT NOT NULL,
            version TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT NOT NULL,
            from_revision INTEGER,
            to_revision INTEGER NOT NULL CHECK (to_revision >= 0),
            reason TEXT NOT NULL,
            event_id TEXT NOT NULL UNIQUE,
            occurred_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            FOREIGN KEY (dna_id, version) REFERENCES dna_definition(dna_id, version)
        )
        """,
        "CREATE INDEX idx_dna_transition_identity ON dna_transition(dna_id, version, occurred_at)",
        """
        CREATE TRIGGER dna_transition_no_update
        BEFORE UPDATE ON dna_transition
        BEGIN
            SELECT RAISE(ABORT, 'dna_transition is append-only');
        END
        """,
        """
        CREATE TRIGGER dna_transition_no_delete
        BEFORE DELETE ON dna_transition
        BEGIN
            SELECT RAISE(ABORT, 'dna_transition is append-only');
        END
        """,
    ),
)


_DNA_FITNESS = Migration(
    "013_dna_fitness",
    (
        """
        CREATE TABLE dna_fitness_observation (
            observation_id TEXT PRIMARY KEY,
            evaluation_id TEXT NOT NULL UNIQUE REFERENCES outcome_evaluation(evaluation_id),
            task_id TEXT NOT NULL REFERENCES task(task_id),
            dna_id TEXT NOT NULL,
            version TEXT NOT NULL,
            content_digest TEXT NOT NULL,
            window_id TEXT NOT NULL,
            successful INTEGER NOT NULL CHECK (successful IN (0, 1)),
            evidence_score REAL NOT NULL CHECK (evidence_score BETWEEN 0 AND 1),
            user_value_score REAL NOT NULL CHECK (user_value_score BETWEEN 0 AND 1),
            cost_minor INTEGER NOT NULL CHECK (cost_minor >= 0),
            latency_ms INTEGER NOT NULL CHECK (latency_ms >= 0),
            stable INTEGER NOT NULL CHECK (stable IN (0, 1)),
            risk_violations_json TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            payload_digest TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            FOREIGN KEY (dna_id, version) REFERENCES dna_definition(dna_id, version)
        )
        """,
        """
        CREATE INDEX idx_dna_fitness_observation_window
        ON dna_fitness_observation(dna_id, version, window_id, observed_at)
        """,
        """
        CREATE TABLE dna_fitness_snapshot (
            dna_id TEXT NOT NULL,
            version TEXT NOT NULL,
            content_digest TEXT NOT NULL,
            window_id TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            sample_count INTEGER NOT NULL CHECK (sample_count >= 0),
            success_rate REAL NOT NULL CHECK (success_rate BETWEEN 0 AND 1),
            success_confidence_lower REAL NOT NULL CHECK (success_confidence_lower BETWEEN 0 AND 1),
            evidence_score REAL NOT NULL CHECK (evidence_score BETWEEN 0 AND 1),
            user_value_score REAL NOT NULL CHECK (user_value_score BETWEEN 0 AND 1),
            average_cost_minor REAL NOT NULL CHECK (average_cost_minor >= 0),
            average_latency_ms REAL NOT NULL CHECK (average_latency_ms >= 0),
            p95_latency_ms INTEGER NOT NULL CHECK (p95_latency_ms >= 0),
            stability_rate REAL NOT NULL CHECK (stability_rate BETWEEN 0 AND 1),
            risk_rate REAL NOT NULL CHECK (risk_rate BETWEEN 0 AND 1),
            readiness TEXT NOT NULL CHECK (readiness IN (
                'COLLECTING','OBSERVING','READY','RISK_BLOCKED'
            )),
            projected_at TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision >= 1),
            PRIMARY KEY (dna_id, version, window_id),
            FOREIGN KEY (dna_id, version) REFERENCES dna_definition(dna_id, version)
        )
        """,
        """
        CREATE TRIGGER dna_fitness_observation_no_update
        BEFORE UPDATE ON dna_fitness_observation
        BEGIN
            SELECT RAISE(ABORT, 'dna_fitness_observation is append-only');
        END
        """,
        """
        CREATE TRIGGER dna_fitness_observation_no_delete
        BEFORE DELETE ON dna_fitness_observation
        BEGIN
            SELECT RAISE(ABORT, 'dna_fitness_observation is append-only');
        END
        """,
    ),
)


_DNA_EXPERIENCE_DATASET = Migration(
    "014_dna_experience_dataset",
    (
        """
        CREATE TABLE dna_experience_dataset (
            dataset_id TEXT NOT NULL,
            version TEXT NOT NULL,
            builder_version TEXT NOT NULL,
            spec_json TEXT NOT NULL,
            manifest_digest TEXT NOT NULL,
            sample_count INTEGER NOT NULL CHECK (sample_count >= 1),
            train_count INTEGER NOT NULL CHECK (train_count >= 0),
            validation_count INTEGER NOT NULL CHECK (validation_count >= 0),
            test_count INTEGER NOT NULL CHECK (test_count >= 0),
            created_at TEXT NOT NULL,
            sealed_at TEXT NOT NULL,
            PRIMARY KEY (dataset_id, version),
            UNIQUE (dataset_id, manifest_digest)
        )
        """,
        """
        CREATE TABLE dna_experience_sample (
            dataset_id TEXT NOT NULL,
            dataset_version TEXT NOT NULL,
            sample_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            split TEXT NOT NULL CHECK (split IN ('TRAIN','VALIDATION','TEST')),
            cohort TEXT NOT NULL CHECK (cohort IN ('BASELINE','CANDIDATE')),
            dna_id TEXT NOT NULL,
            dna_version TEXT NOT NULL,
            content_digest TEXT NOT NULL,
            evaluation_id TEXT NOT NULL,
            observation_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            sample_digest TEXT NOT NULL,
            document_json TEXT NOT NULL,
            PRIMARY KEY (dataset_id, dataset_version, sample_id),
            UNIQUE (dataset_id, dataset_version, ordinal),
            UNIQUE (dataset_id, dataset_version, evaluation_id),
            FOREIGN KEY (dataset_id, dataset_version)
                REFERENCES dna_experience_dataset(dataset_id, version),
            FOREIGN KEY (dna_id, dna_version) REFERENCES dna_definition(dna_id, version),
            FOREIGN KEY (evaluation_id) REFERENCES outcome_evaluation(evaluation_id),
            FOREIGN KEY (observation_id) REFERENCES dna_fitness_observation(observation_id)
        )
        """,
        """
        CREATE INDEX idx_dna_experience_sample_split
        ON dna_experience_sample(dataset_id, dataset_version, split, ordinal)
        """,
        """
        CREATE TRIGGER dna_experience_dataset_no_update
        BEFORE UPDATE ON dna_experience_dataset
        BEGIN
            SELECT RAISE(ABORT, 'dna_experience_dataset is immutable');
        END
        """,
        """
        CREATE TRIGGER dna_experience_dataset_no_delete
        BEFORE DELETE ON dna_experience_dataset
        BEGIN
            SELECT RAISE(ABORT, 'dna_experience_dataset is immutable');
        END
        """,
        """
        CREATE TRIGGER dna_experience_sample_no_update
        BEFORE UPDATE ON dna_experience_sample
        BEGIN
            SELECT RAISE(ABORT, 'dna_experience_sample is immutable');
        END
        """,
        """
        CREATE TRIGGER dna_experience_sample_no_delete
        BEFORE DELETE ON dna_experience_sample
        BEGIN
            SELECT RAISE(ABORT, 'dna_experience_sample is immutable');
        END
        """,
    ),
)


_DNA_CANDIDATE_PROPOSAL = Migration(
    "015_dna_candidate_proposal",
    (
        """
        CREATE TABLE dna_candidate_proposal (
            proposal_id TEXT PRIMARY KEY,
            mode TEXT NOT NULL CHECK (mode IN ('MUTATION','CROSSOVER')),
            candidate_dna_id TEXT NOT NULL,
            candidate_version TEXT NOT NULL,
            candidate_content_digest TEXT NOT NULL,
            base_dna_id TEXT NOT NULL,
            base_version TEXT NOT NULL,
            base_content_digest TEXT NOT NULL,
            donor_dna_id TEXT,
            donor_version TEXT,
            donor_content_digest TEXT,
            dataset_id TEXT NOT NULL,
            dataset_version TEXT NOT NULL,
            dataset_manifest_digest TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            hypothesis TEXT NOT NULL,
            operations_json TEXT NOT NULL,
            candidate_document_json TEXT NOT NULL,
            proposal_digest TEXT NOT NULL,
            created_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            UNIQUE (candidate_dna_id, candidate_content_digest),
            FOREIGN KEY (base_dna_id, base_version)
                REFERENCES dna_definition(dna_id, version),
            FOREIGN KEY (donor_dna_id, donor_version)
                REFERENCES dna_definition(dna_id, version),
            FOREIGN KEY (dataset_id, dataset_version)
                REFERENCES dna_experience_dataset(dataset_id, version)
        )
        """,
        """
        CREATE INDEX idx_dna_candidate_dataset
        ON dna_candidate_proposal(dataset_id, dataset_version, created_at)
        """,
        """
        CREATE TRIGGER dna_candidate_proposal_no_update
        BEFORE UPDATE ON dna_candidate_proposal
        BEGIN
            SELECT RAISE(ABORT, 'dna_candidate_proposal is append-only');
        END
        """,
        """
        CREATE TRIGGER dna_candidate_proposal_no_delete
        BEFORE DELETE ON dna_candidate_proposal
        BEGIN
            SELECT RAISE(ABORT, 'dna_candidate_proposal is append-only');
        END
        """,
    ),
)


_DNA_SANDBOX_REPLAY = Migration(
    "016_dna_sandbox_replay",
    (
        """
        CREATE TABLE dna_replay_run (
            replay_id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL REFERENCES dna_candidate_proposal(proposal_id),
            dataset_id TEXT NOT NULL,
            dataset_version TEXT NOT NULL,
            dataset_manifest_digest TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('PASSED','FAILED')),
            case_count INTEGER NOT NULL CHECK (case_count >= 1),
            fault_case_count INTEGER NOT NULL CHECK (fault_case_count >= 0),
            report_json TEXT NOT NULL,
            report_digest TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            FOREIGN KEY (dataset_id, dataset_version)
                REFERENCES dna_experience_dataset(dataset_id, version)
        )
        """,
        """
        CREATE TABLE dna_replay_case (
            replay_id TEXT NOT NULL REFERENCES dna_replay_run(replay_id),
            sample_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            split TEXT NOT NULL CHECK (split IN ('VALIDATION','TEST')),
            virtual_time TEXT NOT NULL,
            deterministic_seed TEXT NOT NULL,
            fault TEXT NOT NULL CHECK (fault IN (
                'NONE','TIMEOUT','SKILL_FAILURE','CORRUPT_OUTPUT','CANCELLED'
            )),
            parent_measurement_json TEXT NOT NULL,
            candidate_measurement_json TEXT NOT NULL,
            parent_deterministic INTEGER NOT NULL CHECK (parent_deterministic IN (0, 1)),
            candidate_deterministic INTEGER NOT NULL CHECK (candidate_deterministic IN (0, 1)),
            case_digest TEXT NOT NULL,
            PRIMARY KEY (replay_id, sample_id)
        )
        """,
        """
        CREATE TRIGGER dna_replay_run_no_update
        BEFORE UPDATE ON dna_replay_run
        BEGIN
            SELECT RAISE(ABORT, 'dna_replay_run is append-only');
        END
        """,
        """
        CREATE TRIGGER dna_replay_run_no_delete
        BEFORE DELETE ON dna_replay_run
        BEGIN
            SELECT RAISE(ABORT, 'dna_replay_run is append-only');
        END
        """,
        """
        CREATE TRIGGER dna_replay_case_no_update
        BEFORE UPDATE ON dna_replay_case
        BEGIN
            SELECT RAISE(ABORT, 'dna_replay_case is append-only');
        END
        """,
        """
        CREATE TRIGGER dna_replay_case_no_delete
        BEFORE DELETE ON dna_replay_case
        BEGIN
            SELECT RAISE(ABORT, 'dna_replay_case is append-only');
        END
        """,
    ),
)


_DNA_POPULATION_SELECTION = Migration(
    "017_dna_population_selection",
    (
        """
        CREATE TABLE dna_selection_run (
            selection_id TEXT PRIMARY KEY,
            policy_version TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            population_size INTEGER NOT NULL CHECK (population_size >= 1),
            survivor_count INTEGER NOT NULL CHECK (survivor_count >= 1),
            report_json TEXT NOT NULL,
            report_digest TEXT NOT NULL,
            selected_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE dna_selection_member (
            selection_id TEXT NOT NULL REFERENCES dna_selection_run(selection_id),
            proposal_id TEXT NOT NULL,
            replay_id TEXT NOT NULL REFERENCES dna_replay_run(replay_id),
            content_digest TEXT NOT NULL,
            disposition TEXT NOT NULL CHECK (disposition IN (
                'SELECTED','DOMINATED','DUPLICATE','HARD_REJECTED','CAPACITY'
            )),
            pareto_rank INTEGER,
            novelty_score REAL NOT NULL,
            vector_json TEXT NOT NULL,
            reasons_json TEXT NOT NULL,
            member_digest TEXT NOT NULL,
            PRIMARY KEY (selection_id, proposal_id)
        )
        """,
        """CREATE TRIGGER dna_selection_run_no_update BEFORE UPDATE ON dna_selection_run
        BEGIN SELECT RAISE(ABORT, 'dna_selection_run is append-only'); END""",
        """CREATE TRIGGER dna_selection_run_no_delete BEFORE DELETE ON dna_selection_run
        BEGIN SELECT RAISE(ABORT, 'dna_selection_run is append-only'); END""",
        """CREATE TRIGGER dna_selection_member_no_update BEFORE UPDATE ON dna_selection_member
        BEGIN SELECT RAISE(ABORT, 'dna_selection_member is append-only'); END""",
        """CREATE TRIGGER dna_selection_member_no_delete BEFORE DELETE ON dna_selection_member
        BEGIN SELECT RAISE(ABORT, 'dna_selection_member is append-only'); END""",
    ),
)


_DNA_PROMOTION = Migration(
    "018_dna_shadow_canary_promotion",
    (
        """
        CREATE TABLE dna_promotion_campaign (
            campaign_id TEXT PRIMARY KEY,
            selection_id TEXT NOT NULL REFERENCES dna_selection_run(selection_id),
            proposal_id TEXT NOT NULL,
            dna_id TEXT NOT NULL,
            dna_version TEXT NOT NULL,
            content_digest TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            stage TEXT NOT NULL CHECK (stage IN (
                'SHADOW','CANARY','ACTIVE','ROLLED_BACK','STOPPED'
            )),
            baseline_version TEXT,
            stage_started_at TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision >= 0),
            request_digest TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            UNIQUE(selection_id, proposal_id),
            FOREIGN KEY (dna_id,dna_version) REFERENCES dna_definition(dna_id,version)
        )
        """,
        """
        CREATE TABLE dna_promotion_observation (
            observation_id TEXT PRIMARY KEY,
            campaign_id TEXT NOT NULL REFERENCES dna_promotion_campaign(campaign_id),
            stage TEXT NOT NULL CHECK (stage IN ('SHADOW','CANARY','ACTIVE')),
            successful INTEGER NOT NULL CHECK (successful IN (0,1)),
            stable INTEGER NOT NULL CHECK (stable IN (0,1)),
            risk_violations_json TEXT NOT NULL,
            duplicate_side_effect INTEGER NOT NULL CHECK (duplicate_side_effect IN (0,1)),
            permission_expanded INTEGER NOT NULL CHECK (permission_expanded IN (0,1)),
            recovery_failed INTEGER NOT NULL CHECK (recovery_failed IN (0,1)),
            observed_at TEXT NOT NULL,
            observation_digest TEXT NOT NULL,
            correlation_id TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE dna_promotion_event (
            event_id TEXT PRIMARY KEY,
            campaign_id TEXT NOT NULL REFERENCES dna_promotion_campaign(campaign_id),
            from_stage TEXT,
            to_stage TEXT NOT NULL,
            reason TEXT NOT NULL,
            campaign_revision INTEGER NOT NULL,
            occurred_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL
        )
        """,
        """CREATE TRIGGER dna_promotion_observation_no_update
        BEFORE UPDATE ON dna_promotion_observation
        BEGIN SELECT RAISE(ABORT, 'dna_promotion_observation is append-only'); END""",
        """CREATE TRIGGER dna_promotion_observation_no_delete
        BEFORE DELETE ON dna_promotion_observation
        BEGIN SELECT RAISE(ABORT, 'dna_promotion_observation is append-only'); END""",
        """CREATE TRIGGER dna_promotion_event_no_update BEFORE UPDATE ON dna_promotion_event
        BEGIN SELECT RAISE(ABORT, 'dna_promotion_event is append-only'); END""",
        """CREATE TRIGGER dna_promotion_event_no_delete BEFORE DELETE ON dna_promotion_event
        BEGIN SELECT RAISE(ABORT, 'dna_promotion_event is append-only'); END""",
    ),
)


_DNA_LINEAGE_EXPLANATION = Migration(
    "019_dna_lineage_explanation",
    (
        """
        CREATE TABLE dna_explanation (
            explanation_id TEXT PRIMARY KEY,
            dna_id TEXT NOT NULL,
            dna_version TEXT NOT NULL,
            content_digest TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            document_json TEXT NOT NULL,
            explanation_digest TEXT NOT NULL,
            explained_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            FOREIGN KEY (dna_id,dna_version) REFERENCES dna_definition(dna_id,version)
        )
        """,
        """CREATE TRIGGER dna_explanation_no_update BEFORE UPDATE ON dna_explanation
        BEGIN SELECT RAISE(ABORT, 'dna_explanation is append-only'); END""",
        """CREATE TRIGGER dna_explanation_no_delete BEFORE DELETE ON dna_explanation
        BEGIN SELECT RAISE(ABORT, 'dna_explanation is append-only'); END""",
    ),
)


_AGENT_DNA = Migration(
    "020_agent_dna",
    (
        """
        CREATE TABLE agent_dna_definition (
            dna_id TEXT NOT NULL,
            version TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN (
                'CANDIDATE','VALIDATED','SHADOW','CANARY','ACTIVE','DEPRECATED','RETIRED'
            )),
            content_digest TEXT NOT NULL,
            envelope_digest TEXT NOT NULL,
            document_json TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            PRIMARY KEY (dna_id,version),
            UNIQUE (dna_id,content_digest)
        )
        """,
        """CREATE UNIQUE INDEX idx_agent_dna_one_active
        ON agent_dna_definition(dna_id) WHERE status='ACTIVE'""",
        """
        CREATE TABLE agent_dna_workflow_ref (
            agent_dna_id TEXT NOT NULL,
            agent_version TEXT NOT NULL,
            role TEXT NOT NULL,
            workflow_dna_id TEXT NOT NULL,
            workflow_version TEXT NOT NULL,
            workflow_content_digest TEXT NOT NULL,
            PRIMARY KEY (agent_dna_id,agent_version,role),
            FOREIGN KEY (agent_dna_id,agent_version)
                REFERENCES agent_dna_definition(dna_id,version),
            FOREIGN KEY (workflow_dna_id,workflow_version)
                REFERENCES dna_definition(dna_id,version)
        )
        """,
        """
        CREATE TABLE agent_dna_transition (
            event_id TEXT PRIMARY KEY,
            dna_id TEXT NOT NULL,
            version TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT NOT NULL,
            from_revision INTEGER,
            to_revision INTEGER NOT NULL,
            reason TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            FOREIGN KEY (dna_id,version) REFERENCES agent_dna_definition(dna_id,version)
        )
        """,
        """CREATE TRIGGER agent_dna_workflow_ref_no_update
        BEFORE UPDATE ON agent_dna_workflow_ref
        BEGIN SELECT RAISE(ABORT, 'agent_dna_workflow_ref is immutable'); END""",
        """CREATE TRIGGER agent_dna_workflow_ref_no_delete
        BEFORE DELETE ON agent_dna_workflow_ref
        BEGIN SELECT RAISE(ABORT, 'agent_dna_workflow_ref is immutable'); END""",
        """CREATE TRIGGER agent_dna_transition_no_update BEFORE UPDATE ON agent_dna_transition
        BEGIN SELECT RAISE(ABORT, 'agent_dna_transition is append-only'); END""",
        """CREATE TRIGGER agent_dna_transition_no_delete BEFORE DELETE ON agent_dna_transition
        BEGIN SELECT RAISE(ABORT, 'agent_dna_transition is append-only'); END""",
    ),
)


_ORGANIZATION_DNA = Migration(
    "021_organization_dna",
    (
        """
        CREATE TABLE organization_dna_definition (
            dna_id TEXT NOT NULL,
            version TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN (
                'CANDIDATE','VALIDATED','ACTIVE','DEPRECATED','RETIRED'
            )),
            content_digest TEXT NOT NULL,
            envelope_digest TEXT NOT NULL,
            document_json TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            PRIMARY KEY (dna_id,version),
            UNIQUE (dna_id,content_digest)
        )
        """,
        """CREATE UNIQUE INDEX idx_organization_dna_one_active
        ON organization_dna_definition(dna_id) WHERE status='ACTIVE'""",
        """
        CREATE TABLE organization_dna_member (
            organization_dna_id TEXT NOT NULL,
            organization_version TEXT NOT NULL,
            role TEXT NOT NULL,
            agent_dna_id TEXT NOT NULL,
            agent_version TEXT NOT NULL,
            agent_content_digest TEXT NOT NULL,
            responsibilities_json TEXT NOT NULL,
            priority INTEGER NOT NULL CHECK (priority >= 0),
            PRIMARY KEY (organization_dna_id,organization_version,role),
            FOREIGN KEY (organization_dna_id,organization_version)
                REFERENCES organization_dna_definition(dna_id,version),
            FOREIGN KEY (agent_dna_id,agent_version)
                REFERENCES agent_dna_definition(dna_id,version)
        )
        """,
        """
        CREATE TABLE organization_dna_transition (
            event_id TEXT PRIMARY KEY,
            dna_id TEXT NOT NULL,
            version TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT NOT NULL,
            from_revision INTEGER,
            to_revision INTEGER NOT NULL,
            reason TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            FOREIGN KEY (dna_id,version)
                REFERENCES organization_dna_definition(dna_id,version)
        )
        """,
        """CREATE TRIGGER organization_dna_member_no_update
        BEFORE UPDATE ON organization_dna_member
        BEGIN SELECT RAISE(ABORT, 'organization_dna_member is immutable'); END""",
        """CREATE TRIGGER organization_dna_member_no_delete
        BEFORE DELETE ON organization_dna_member
        BEGIN SELECT RAISE(ABORT, 'organization_dna_member is immutable'); END""",
        """CREATE TRIGGER organization_dna_transition_no_update
        BEFORE UPDATE ON organization_dna_transition
        BEGIN SELECT RAISE(ABORT, 'organization_dna_transition is append-only'); END""",
        """CREATE TRIGGER organization_dna_transition_no_delete
        BEFORE DELETE ON organization_dna_transition
        BEGIN SELECT RAISE(ABORT, 'organization_dna_transition is append-only'); END""",
    ),
)


_DNA_EXECUTION_IDENTITY = Migration(
    "022_dna_execution_identity",
    (
        """
        CREATE TABLE dna_execution_context (
            context_digest TEXT PRIMARY KEY,
            correlation_id TEXT NOT NULL,
            plan_id TEXT NOT NULL UNIQUE REFERENCES plan(plan_id),
            decision_id TEXT NOT NULL UNIQUE REFERENCES plan_decision(decision_id),
            grant_id TEXT NOT NULL UNIQUE REFERENCES execution_grant(grant_id),
            task_id TEXT NOT NULL UNIQUE REFERENCES task(task_id),
            run_id TEXT NOT NULL UNIQUE REFERENCES workflow_run(run_id),
            episode_id TEXT NOT NULL UNIQUE REFERENCES episode(episode_id),
            evaluation_id TEXT NOT NULL UNIQUE REFERENCES outcome_evaluation(evaluation_id),
            organization_dna_id TEXT NOT NULL,
            organization_version TEXT NOT NULL,
            organization_content_digest TEXT NOT NULL,
            organization_role TEXT NOT NULL,
            agent_dna_id TEXT NOT NULL,
            agent_version TEXT NOT NULL,
            agent_content_digest TEXT NOT NULL,
            workflow_dna_id TEXT NOT NULL,
            workflow_version TEXT NOT NULL,
            workflow_content_digest TEXT NOT NULL,
            context_json TEXT NOT NULL,
            FOREIGN KEY (organization_dna_id,organization_version)
                REFERENCES organization_dna_definition(dna_id,version),
            FOREIGN KEY (agent_dna_id,agent_version)
                REFERENCES agent_dna_definition(dna_id,version),
            FOREIGN KEY (workflow_dna_id,workflow_version)
                REFERENCES dna_definition(dna_id,version)
        )
        """,
        """CREATE TRIGGER dna_execution_context_no_update
        BEFORE UPDATE ON dna_execution_context
        BEGIN SELECT RAISE(ABORT, 'dna_execution_context is append-only'); END""",
        """CREATE TRIGGER dna_execution_context_no_delete
        BEFORE DELETE ON dna_execution_context
        BEGIN SELECT RAISE(ABORT, 'dna_execution_context is append-only'); END""",
        "CREATE INDEX idx_dna_execution_correlation ON dna_execution_context(correlation_id)",
    ),
)


DEFAULT_MIGRATIONS = (
    _INITIAL_SCHEMA,
    _INBOX_BUSINESS_DEDUP,
    _SCHEDULER_CHECKPOINT,
    _WORKFLOW_RUN_TRANSITIONS,
    _NODE_INLINE_OUTPUT,
    _PLAN_GRANT_LIFECYCLE,
    _DELAYED_OUTCOME_LEDGER,
    _REST_REPAIR_RUN,
    _SEMANTIC_MEMORY_CANDIDATES,
    _LOCAL_NOTIFICATION_DELIVERY,
    _INSIGHT_DELIVERY_PREFERENCES,
    _DNA_REGISTRY,
    _DNA_FITNESS,
    _DNA_EXPERIENCE_DATASET,
    _DNA_CANDIDATE_PROPOSAL,
    _DNA_SANDBOX_REPLAY,
    _DNA_POPULATION_SELECTION,
    _DNA_PROMOTION,
    _DNA_LINEAGE_EXPLANATION,
    _AGENT_DNA,
    _ORGANIZATION_DNA,
    _DNA_EXECUTION_IDENTITY,
)
