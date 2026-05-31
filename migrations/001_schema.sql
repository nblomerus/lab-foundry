-- LabFoundry — consolidated baseline schema (single source of truth).
--
-- Generated via `pg_dump --schema-only` from a verified clean apply of the
-- former incremental migrations 001-015. Those were collapsed here after the
-- git-history reset: the old set did an in-place boardroom->research-lab
-- reontology (008) that only applied under error-tolerant `make migrate` and
-- could NOT boot a fresh DB cleanly. This baseline does.
--
-- Applied fresh by docker-entrypoint-initdb.d (compose) and `make migrate`.
-- Requires the pgvector image (see CREATE EXTENSION vector below).

--
-- PostgreSQL database dump
--


-- Dumped from database version 16.14 (Debian 16.14-1.pgdg12+1)
-- Dumped by pg_dump version 16.14 (Debian 16.14-1.pgdg12+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;


--
-- Name: EXTENSION pg_trgm; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pg_trgm IS 'text similarity measurement and index searching based on trigrams';


--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


--
-- Name: claim_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.claim_status AS ENUM (
    'proposed',
    'tested',
    'weakly_supported',
    'replicated',
    'invalidated',
    'merged'
);


--
-- Name: document_kind; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.document_kind AS ENUM (
    'paper',
    'media',
    'dataset',
    'web',
    'code',
    'note'
);


--
-- Name: document_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.document_status AS ENUM (
    'quarantined',
    'certified',
    'blocked'
);


--
-- Name: event_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.event_status AS ENUM (
    'pending',
    'consumed',
    'failed',
    'suppressed'
);


--
-- Name: lesson_source; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.lesson_source AS ENUM (
    'reflection',
    'audit_pattern',
    'adversary_pattern',
    'user_injected',
    'tool_misuse'
);


--
-- Name: lesson_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.lesson_status AS ENUM (
    'probationary',
    'active',
    'retired'
);


--
-- Name: model_tier; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.model_tier AS ENUM (
    'reasoning',
    'workhorse',
    'fast',
    'code'
);


--
-- Name: phase; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.phase AS ENUM (
    'frame',
    'hypothesize',
    'experiment',
    'validate',
    'write',
    'submit'
);


--
-- Name: task_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.task_status AS ENUM (
    'pending',
    'running',
    'completed',
    'failed',
    'halted'
);


--
-- Name: trust_state; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.trust_state AS ENUM (
    'provisional',
    'certified',
    'decayed',
    'quarantined'
);


--
-- Name: trust_tier; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.trust_tier AS ENUM (
    'quarantined',
    'user_asserted',
    'web_unknown',
    'web_reputable',
    'official_repo',
    'preprint',
    'peer_reviewed'
);


--
-- Name: decay_lessons(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.decay_lessons() RETURNS TABLE(lesson_id bigint, action text)
    LANGUAGE plpgsql
    AS $$
BEGIN
    RETURN QUERY
    WITH stale AS (
        SELECT l.id
        FROM lessons l
        LEFT JOIN lesson_applications la
               ON la.lesson_id = l.id AND la.outcome = 'supportive'
        WHERE l.status = 'probationary'
          AND l.created_at < NOW() - INTERVAL '14 days'
        GROUP BY l.id
        HAVING COUNT(la.id) = 0
    ),
    retired AS (
        UPDATE lessons l
        SET status = 'retired',
            retired_at = NOW(),
            retired_reason = 'decayed: 14d probationary with 0 supportive applications'
        FROM stale
        WHERE l.id = stale.id
        RETURNING l.id
    )
    SELECT retired.id, 'decayed'::TEXT FROM retired;
END;
$$;


--
-- Name: emit_queue_empty_if_drained(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.emit_queue_empty_if_drained() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    remaining INT;
BEGIN
    IF OLD.status <> 'pending' OR NEW.status = 'pending' THEN
        RETURN NEW;
    END IF;
    SELECT COUNT(*) INTO remaining
    FROM tasks
    WHERE department = NEW.department AND status = 'pending';

    IF remaining = 0 THEN
        INSERT INTO events (event_type, target_type, payload, dedup_key)
        VALUES (
            'queue.empty',
            'queue',
            jsonb_build_object('department', NEW.department),
            'queueempty-' || NEW.department || '-' || EXTRACT(EPOCH FROM NOW())::bigint::text
        )
        ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING;
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: emit_task_created(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.emit_task_created() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.status <> 'pending' THEN
        RETURN NEW;
    END IF;
    INSERT INTO events (event_type, target_type, target_id, payload, dedup_key)
    VALUES (
        'task.created',
        'task',
        NEW.id,
        jsonb_build_object(
            'department', NEW.department,
            'task_type',  NEW.task_type,
            'priority',   NEW.priority,
            'thesis_id',  NEW.thesis_id
        ),
        'taskcreate-' || NEW.id::text
    )
    ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING;
    RETURN NEW;
END;
$$;


--
-- Name: notify_event(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.notify_event() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM pg_notify('events', json_build_object(
        'id', NEW.id,
        'type', NEW.event_type,
        'target_type', NEW.target_type,
        'target_id', NEW.target_id,
        'session_id', NEW.session_id
    )::text);
    RETURN NEW;
END;
$$;


--
-- Name: reconcile_lessons(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.reconcile_lessons() RETURNS TABLE(lesson_id bigint, action text, new_status public.lesson_status)
    LANGUAGE plpgsql
    AS $$
BEGIN
    RETURN QUERY
    WITH lesson_stats AS (
        SELECT
            l.id,
            l.status,
            COUNT(*) FILTER (WHERE la.outcome = 'supportive') AS supportive,
            COUNT(*) FILTER (WHERE la.outcome = 'contradicting') AS contradicting,
            COUNT(*) FILTER (WHERE la.outcome IS NOT NULL) AS judged
        FROM lessons l
        LEFT JOIN lesson_applications la ON la.lesson_id = l.id
        WHERE l.status IN ('probationary', 'active')
        GROUP BY l.id, l.status
    ),
    promotions AS (
        UPDATE lessons l
        SET status = 'active',
            promoted_at = NOW(),
            promotion_run_count = ls.supportive::INT,
            confidence = LEAST(0.95, l.confidence + 0.10)
        FROM lesson_stats ls
        WHERE l.id = ls.id
          AND l.status = 'probationary'
          AND ls.supportive >= 5
          AND ls.contradicting <= 1
        RETURNING l.id, 'promoted'::TEXT AS action, l.status
    ),
    retirements AS (
        UPDATE lessons l
        SET status = 'retired',
            retired_at = NOW(),
            retired_reason = format('contradicted by %s runs vs %s supportive',
                                    ls.contradicting, ls.supportive),
            contradiction_run_count = ls.contradicting::INT
        FROM lesson_stats ls
        WHERE l.id = ls.id
          AND ls.contradicting >= 3
          AND ls.contradicting > ls.supportive
        RETURNING l.id, 'retired'::TEXT AS action, l.status
    )
    SELECT * FROM promotions
    UNION ALL
    SELECT * FROM retirements;
END;
$$;


--
-- Name: trigger_reflection_on_dissent(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.trigger_reflection_on_dissent() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    had_dissent BOOLEAN;
BEGIN
    IF NEW.status NOT IN ('completed', 'failed') THEN
        RETURN NEW;
    END IF;
    IF OLD.status = NEW.status THEN
        RETURN NEW;
    END IF;

    had_dissent := EXISTS (
        SELECT 1 FROM events
        WHERE emitted_by_run_id = NEW.id
          AND event_type IN ('audit.slop_detected', 'thesis.invalidated')
    ) OR (
        NEW.invocation_type IN ('adversary.kill_verdict', 'auditor.slop_score')
        AND NEW.status = 'completed'
    );

    IF had_dissent THEN
        INSERT INTO events (
            event_type, target_type, target_id, payload,
            emitted_by_run_id, dedup_key
        )
        VALUES (
            'reflection.requested',
            'agent_run',
            NEW.id,
            jsonb_build_object('invocation_type', NEW.invocation_type),
            NEW.id,
            'reflect-' || NEW.id::TEXT
        )
        ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING;
    END IF;

    RETURN NEW;
END;
$$;


--
-- Name: trust_rank(public.trust_tier); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.trust_rank(t public.trust_tier) RETURNS integer
    LANGUAGE sql IMMUTABLE
    AS $$ SELECT CASE t
    WHEN 'quarantined'    THEN 0 WHEN 'user_asserted' THEN 1
    WHEN 'web_unknown'    THEN 2 WHEN 'web_reputable'  THEN 3
    WHEN 'official_repo'  THEN 4 WHEN 'preprint'        THEN 5
    WHEN 'peer_reviewed'  THEN 6 END $$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: lessons; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.lessons (
    id bigint NOT NULL,
    applies_to_invocation text NOT NULL,
    applies_when jsonb DEFAULT '{}'::jsonb NOT NULL,
    lesson_text text NOT NULL,
    rationale text,
    derived_from_run_id bigint,
    derived_via public.lesson_source NOT NULL,
    confidence numeric(3,2) DEFAULT 0.40 NOT NULL,
    supersedes bigint,
    superseded_by bigint,
    status public.lesson_status DEFAULT 'probationary'::public.lesson_status NOT NULL,
    promotion_run_count integer DEFAULT 0 NOT NULL,
    contradiction_run_count integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    promoted_at timestamp with time zone,
    retired_at timestamp with time zone,
    retired_reason text,
    CONSTRAINT lessons_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric)))
);


--
-- Name: active_lessons_by_invocation; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.active_lessons_by_invocation AS
 SELECT id,
    applies_to_invocation,
    applies_when,
    lesson_text,
    confidence,
    status,
    promotion_run_count,
    contradiction_run_count
   FROM public.lessons l
  WHERE (status = ANY (ARRAY['probationary'::public.lesson_status, 'active'::public.lesson_status]))
  ORDER BY applies_to_invocation, confidence DESC, promotion_run_count DESC;


--
-- Name: critic_verdicts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.critic_verdicts (
    id bigint NOT NULL,
    thesis_id bigint NOT NULL,
    verdict text NOT NULL,
    confidence numeric(3,2) NOT NULL,
    reasoning text NOT NULL,
    cited_finding_ids bigint[] NOT NULL,
    first_pass_verdict text,
    first_pass_reasoning text,
    revised boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    run_id bigint
);


--
-- Name: adversary_verdicts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.adversary_verdicts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: adversary_verdicts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.adversary_verdicts_id_seq OWNED BY public.critic_verdicts.id;


--
-- Name: agent_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_runs (
    id bigint NOT NULL,
    department text NOT NULL,
    agent_name text NOT NULL,
    invocation_type text NOT NULL,
    model_tier public.model_tier NOT NULL,
    model_name text NOT NULL,
    triggered_by_event_id bigint,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    status text DEFAULT 'running'::text NOT NULL,
    input_token_count integer,
    output_token_count integer,
    cost_usd numeric(8,4),
    error text,
    langfuse_trace_id text,
    input_summary text,
    output_summary text,
    session_id bigint,
    step_name text,
    parent_step_id bigint,
    step_order integer,
    fallback_attempts jsonb DEFAULT '[]'::jsonb NOT NULL,
    expectation text,
    outcome text,
    expectation_met boolean
);


--
-- Name: agent_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_runs_id_seq OWNED BY public.agent_runs.id;


--
-- Name: agent_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_sessions (
    id bigint NOT NULL,
    handler_name text NOT NULL,
    triggered_by_event_id bigint,
    status text DEFAULT 'running'::text NOT NULL,
    mode text DEFAULT 'live'::text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    error text,
    CONSTRAINT agent_sessions_mode_check CHECK ((mode = ANY (ARRAY['live'::text, 'replay'::text]))),
    CONSTRAINT agent_sessions_status_check CHECK ((status = ANY (ARRAY['running'::text, 'completed'::text, 'failed'::text])))
);


--
-- Name: agent_sessions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_sessions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_sessions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_sessions_id_seq OWNED BY public.agent_sessions.id;


--
-- Name: bench_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bench_runs (
    id bigint NOT NULL,
    job_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    invocation_type text NOT NULL,
    tier text,
    thesis_id bigint,
    context_note text,
    prompt_tokens integer,
    prompt_preview text,
    status text DEFAULT 'running'::text NOT NULL,
    results jsonb DEFAULT '[]'::jsonb NOT NULL
);


--
-- Name: bench_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bench_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: bench_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bench_runs_id_seq OWNED BY public.bench_runs.id;


--
-- Name: certifications; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.certifications (
    id bigint NOT NULL,
    document_id bigint NOT NULL,
    decision text NOT NULL,
    from_tier public.trust_tier,
    to_tier public.trust_tier NOT NULL,
    to_state public.trust_state NOT NULL,
    signals jsonb DEFAULT '{}'::jsonb NOT NULL,
    used_llm boolean DEFAULT false NOT NULL,
    reasons text NOT NULL,
    decided_by_run_id bigint,
    requested_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT certifications_decision_check CHECK ((decision = ANY (ARRAY['approve'::text, 'block'::text, 'certify'::text, 'decay'::text, 'recertify'::text])))
);


--
-- Name: certifications_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.certifications_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: certifications_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.certifications_id_seq OWNED BY public.certifications.id;


--
-- Name: chunks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chunks (
    id bigint NOT NULL,
    document_id bigint NOT NULL,
    ordinal integer NOT NULL,
    text text NOT NULL,
    embedding public.vector(768),
    embed_model text,
    token_count integer,
    content_hash text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: chunks_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.chunks_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: chunks_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.chunks_id_seq OWNED BY public.chunks.id;


--
-- Name: claims; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.claims (
    id bigint NOT NULL,
    statement text NOT NULL,
    status public.claim_status DEFAULT 'proposed'::public.claim_status NOT NULL,
    parent_id bigint,
    confidence numeric(3,2) DEFAULT 0.50 NOT NULL,
    confidence_prev numeric(3,2),
    created_by_run_id bigint,
    invalidated_at timestamp with time zone,
    invalidated_by_verdict_id bigint,
    invalidation_reason text,
    last_evidence_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT theses_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric)))
);


--
-- Name: company_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.company_state (
    id integer DEFAULT 1 NOT NULL,
    problem_statement text NOT NULL,
    stance text,
    success_criterion text,
    current_phase public.phase DEFAULT 'frame'::public.phase NOT NULL,
    phase_started_at timestamp with time zone DEFAULT now() NOT NULL,
    bootstrap_at timestamp with time zone DEFAULT now() NOT NULL,
    deadline timestamp with time zone NOT NULL,
    thesis text,
    niche text,
    audience text,
    charter text,
    paused boolean DEFAULT false NOT NULL,
    paused_reason text,
    CONSTRAINT company_state_id_check CHECK ((id = 1))
);


--
-- Name: COLUMN company_state.problem_statement; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.company_state.problem_statement IS 'Research mandate: the problem space the lab is investigating';


--
-- Name: COLUMN company_state.thesis; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.company_state.thesis IS 'Primary claim under investigation';


--
-- Name: COLUMN company_state.niche; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.company_state.niche IS 'Research question or focus area';


--
-- Name: COLUMN company_state.audience; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.company_state.audience IS 'Target publication venue';


--
-- Name: COLUMN company_state.charter; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.company_state.charter IS 'Research plan and methodology';


--
-- Name: cooldowns; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cooldowns (
    id bigint NOT NULL,
    invocation_type text NOT NULL,
    target_type text NOT NULL,
    target_id bigint NOT NULL,
    cooldown_until timestamp with time zone NOT NULL,
    set_by_run_id bigint
);


--
-- Name: cooldowns_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.cooldowns_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: cooldowns_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.cooldowns_id_seq OWNED BY public.cooldowns.id;


--
-- Name: cost_tracking; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cost_tracking (
    day date NOT NULL,
    total_cost_usd numeric(8,4) DEFAULT 0 NOT NULL,
    reasoning_calls integer DEFAULT 0 NOT NULL,
    workhorse_calls integer DEFAULT 0 NOT NULL,
    fast_calls integer DEFAULT 0 NOT NULL,
    code_calls integer DEFAULT 0 NOT NULL,
    cap_reached boolean DEFAULT false NOT NULL
);


--
-- Name: datasets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.datasets (
    id bigint NOT NULL,
    name text NOT NULL,
    url text,
    modality text,
    task text,
    size text,
    license text,
    notes text,
    document_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: datasets_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.datasets_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: datasets_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.datasets_id_seq OWNED BY public.datasets.id;


--
-- Name: deepseek_balance_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.deepseek_balance_log (
    id bigint NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    total_balance numeric(10,4) NOT NULL,
    topped_up numeric(10,4),
    granted numeric(10,4)
);


--
-- Name: deepseek_balance_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.deepseek_balance_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: deepseek_balance_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.deepseek_balance_log_id_seq OWNED BY public.deepseek_balance_log.id;


--
-- Name: documents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.documents (
    id bigint NOT NULL,
    kind public.document_kind NOT NULL,
    title text,
    authors text[] DEFAULT '{}'::text[] NOT NULL,
    source_kind text NOT NULL,
    source_url text,
    canonical_key text NOT NULL,
    doi text,
    arxiv_id text,
    published_at timestamp with time zone,
    ingested_at timestamp with time zone DEFAULT now() NOT NULL,
    license text,
    raw_uri text,
    content_hash text,
    queryable boolean DEFAULT false NOT NULL,
    parse_run_id bigint,
    status public.document_status DEFAULT 'quarantined'::public.document_status NOT NULL,
    trust_tier public.trust_tier DEFAULT 'web_unknown'::public.trust_tier NOT NULL,
    trust_state public.trust_state DEFAULT 'provisional'::public.trust_state NOT NULL,
    provenance jsonb DEFAULT '{}'::jsonb NOT NULL,
    certified_by_run_id bigint,
    certified_at timestamp with time zone,
    last_trust_review_at timestamp with time zone,
    retracted boolean DEFAULT false NOT NULL,
    last_source_push timestamp with time zone,
    CONSTRAINT ck_documents_arxiv CHECK ((arxiv_id <> ''::text)),
    CONSTRAINT ck_documents_doi CHECK ((doi <> ''::text))
);


--
-- Name: documents_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.documents_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: documents_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.documents_id_seq OWNED BY public.documents.id;


--
-- Name: events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.events (
    id bigint NOT NULL,
    event_type text NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    target_type text,
    target_id bigint,
    emitted_at timestamp with time zone DEFAULT now() NOT NULL,
    emitted_by_run_id bigint,
    status public.event_status DEFAULT 'pending'::public.event_status NOT NULL,
    consumed_at timestamp with time zone,
    consumed_by_handler text,
    consumed_run_id bigint,
    suppression_reason text,
    dedup_key text,
    session_id bigint
);


--
-- Name: events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.events_id_seq OWNED BY public.events.id;


--
-- Name: evidence; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.evidence (
    id bigint NOT NULL,
    task_id bigint NOT NULL,
    inquiry_id bigint,
    sub_question_idx integer NOT NULL,
    url text NOT NULL,
    title text,
    quote text NOT NULL,
    claim text NOT NULL,
    stance text NOT NULL,
    confidence numeric(3,2) NOT NULL,
    extract_run_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT evidence_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
    CONSTRAINT evidence_stance_check CHECK ((stance = ANY (ARRAY['supports'::text, 'refutes'::text, 'neutral'::text])))
);


--
-- Name: evidence_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.evidence_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: evidence_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.evidence_id_seq OWNED BY public.evidence.id;


--
-- Name: experiment_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.experiment_runs (
    id bigint NOT NULL,
    task_id bigint NOT NULL,
    inquiry_id bigint,
    kind text NOT NULL,
    params jsonb NOT NULL,
    result jsonb,
    error text,
    status text DEFAULT 'pending'::text NOT NULL,
    interpretation text,
    interpret_run_id bigint,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    CONSTRAINT experiment_runs_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'running'::text, 'completed'::text, 'failed'::text])))
);


--
-- Name: experiment_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.experiment_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: experiment_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.experiment_runs_id_seq OWNED BY public.experiment_runs.id;


--
-- Name: fetch_cache; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fetch_cache (
    url text NOT NULL,
    content text NOT NULL,
    extractor text NOT NULL,
    status_code integer NOT NULL,
    bytes_fetched integer,
    fetched_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL
);


--
-- Name: findings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.findings (
    id bigint NOT NULL,
    task_id bigint NOT NULL,
    claim_id bigint,
    source text,
    url text,
    title text,
    summary text NOT NULL,
    relevance_score numeric(3,1) NOT NULL,
    why_it_matters text,
    audit_score numeric(3,2),
    audit_verdict text,
    supports_thesis boolean,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT findings_relevance_score_check CHECK (((relevance_score >= (1)::numeric) AND (relevance_score <= (10)::numeric)))
);


--
-- Name: findings_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.findings_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: findings_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.findings_id_seq OWNED BY public.findings.id;


--
-- Name: lesson_applications; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.lesson_applications (
    id bigint NOT NULL,
    lesson_id bigint NOT NULL,
    agent_run_id bigint NOT NULL,
    outcome text,
    outcome_judged_at timestamp with time zone,
    outcome_judged_by_run_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: lesson_applications_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.lesson_applications_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: lesson_applications_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.lesson_applications_id_seq OWNED BY public.lesson_applications.id;


--
-- Name: lessons_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.lessons_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: lessons_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.lessons_id_seq OWNED BY public.lessons.id;


--
-- Name: memory_pointers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memory_pointers (
    id bigint NOT NULL,
    entity_type text NOT NULL,
    entity_id bigint NOT NULL,
    zep_session_id text NOT NULL,
    zep_message_uuid text,
    summary text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: memory_pointers_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.memory_pointers_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: memory_pointers_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.memory_pointers_id_seq OWNED BY public.memory_pointers.id;


--
-- Name: objectives; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.objectives (
    id bigint NOT NULL,
    week_start date NOT NULL,
    objective text NOT NULL,
    success_criteria text NOT NULL,
    rationale text,
    claim_id bigint,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    closed_at timestamp with time zone,
    created_by_run_id bigint
);


--
-- Name: objectives_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.objectives_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: objectives_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.objectives_id_seq OWNED BY public.objectives.id;


--
-- Name: phase_transitions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.phase_transitions (
    id bigint NOT NULL,
    from_phase public.phase NOT NULL,
    to_phase public.phase NOT NULL,
    reason text NOT NULL,
    cited_finding_ids bigint[] DEFAULT '{}'::bigint[] NOT NULL,
    cited_claim_ids bigint[] DEFAULT '{}'::bigint[] NOT NULL,
    proposed_by_run_id bigint,
    forced boolean DEFAULT false NOT NULL,
    decided_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: phase_transitions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.phase_transitions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: phase_transitions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.phase_transitions_id_seq OWNED BY public.phase_transitions.id;


--
-- Name: research_inquiries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.research_inquiries (
    id bigint NOT NULL,
    task_id bigint NOT NULL,
    iteration integer DEFAULT 1 NOT NULL,
    question text NOT NULL,
    sub_questions jsonb NOT NULL,
    proposed_experiments jsonb DEFAULT '[]'::jsonb NOT NULL,
    plan_run_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: research_inquiries_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.research_inquiries_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: research_inquiries_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.research_inquiries_id_seq OWNED BY public.research_inquiries.id;


--
-- Name: slop_rate_by_claim; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.slop_rate_by_claim AS
 SELECT claim_id,
    ((count(
        CASE
            WHEN (audit_verdict = 'slop'::text) THEN 1
            ELSE NULL::integer
        END))::double precision / (NULLIF(count(*), 0))::double precision) AS slop_rate,
    count(*) AS window_size,
    max(created_at) AS latest
   FROM public.findings f
  WHERE (created_at > (now() - '24:00:00'::interval))
  GROUP BY claim_id
 HAVING (count(*) >= 5)
  WITH NO DATA;


--
-- Name: tasks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tasks (
    id bigint NOT NULL,
    objective_id bigint,
    claim_id bigint,
    department text NOT NULL,
    task_type text NOT NULL,
    description text NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    priority integer DEFAULT 5 NOT NULL,
    status public.task_status DEFAULT 'pending'::public.task_status NOT NULL,
    claimed_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    result jsonb,
    halt_reason text
);


--
-- Name: tasks_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.tasks_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: tasks_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.tasks_id_seq OWNED BY public.tasks.id;


--
-- Name: theses_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.theses_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: theses_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.theses_id_seq OWNED BY public.claims.id;


--
-- Name: tool_description_versions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tool_description_versions (
    id bigint NOT NULL,
    server_name text NOT NULL,
    tool_name text NOT NULL,
    version integer NOT NULL,
    description text NOT NULL,
    parameters_schema jsonb NOT NULL,
    derived_from_lesson_id bigint,
    is_current boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: tool_description_versions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.tool_description_versions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: tool_description_versions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.tool_description_versions_id_seq OWNED BY public.tool_description_versions.id;


--
-- Name: user_overrides; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_overrides (
    id bigint NOT NULL,
    override_type text NOT NULL,
    target_type text,
    target_id bigint,
    note text,
    applied_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: user_overrides_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.user_overrides_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: user_overrides_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.user_overrides_id_seq OWNED BY public.user_overrides.id;


--
-- Name: agent_runs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_runs ALTER COLUMN id SET DEFAULT nextval('public.agent_runs_id_seq'::regclass);


--
-- Name: agent_sessions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_sessions ALTER COLUMN id SET DEFAULT nextval('public.agent_sessions_id_seq'::regclass);


--
-- Name: bench_runs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bench_runs ALTER COLUMN id SET DEFAULT nextval('public.bench_runs_id_seq'::regclass);


--
-- Name: certifications id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.certifications ALTER COLUMN id SET DEFAULT nextval('public.certifications_id_seq'::regclass);


--
-- Name: chunks id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunks ALTER COLUMN id SET DEFAULT nextval('public.chunks_id_seq'::regclass);


--
-- Name: claims id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.claims ALTER COLUMN id SET DEFAULT nextval('public.theses_id_seq'::regclass);


--
-- Name: cooldowns id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cooldowns ALTER COLUMN id SET DEFAULT nextval('public.cooldowns_id_seq'::regclass);


--
-- Name: critic_verdicts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.critic_verdicts ALTER COLUMN id SET DEFAULT nextval('public.adversary_verdicts_id_seq'::regclass);


--
-- Name: datasets id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.datasets ALTER COLUMN id SET DEFAULT nextval('public.datasets_id_seq'::regclass);


--
-- Name: deepseek_balance_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deepseek_balance_log ALTER COLUMN id SET DEFAULT nextval('public.deepseek_balance_log_id_seq'::regclass);


--
-- Name: documents id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents ALTER COLUMN id SET DEFAULT nextval('public.documents_id_seq'::regclass);


--
-- Name: events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events ALTER COLUMN id SET DEFAULT nextval('public.events_id_seq'::regclass);


--
-- Name: evidence id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evidence ALTER COLUMN id SET DEFAULT nextval('public.evidence_id_seq'::regclass);


--
-- Name: experiment_runs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.experiment_runs ALTER COLUMN id SET DEFAULT nextval('public.experiment_runs_id_seq'::regclass);


--
-- Name: findings id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.findings ALTER COLUMN id SET DEFAULT nextval('public.findings_id_seq'::regclass);


--
-- Name: lesson_applications id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lesson_applications ALTER COLUMN id SET DEFAULT nextval('public.lesson_applications_id_seq'::regclass);


--
-- Name: lessons id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lessons ALTER COLUMN id SET DEFAULT nextval('public.lessons_id_seq'::regclass);


--
-- Name: memory_pointers id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_pointers ALTER COLUMN id SET DEFAULT nextval('public.memory_pointers_id_seq'::regclass);


--
-- Name: objectives id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.objectives ALTER COLUMN id SET DEFAULT nextval('public.objectives_id_seq'::regclass);


--
-- Name: phase_transitions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phase_transitions ALTER COLUMN id SET DEFAULT nextval('public.phase_transitions_id_seq'::regclass);


--
-- Name: research_inquiries id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.research_inquiries ALTER COLUMN id SET DEFAULT nextval('public.research_inquiries_id_seq'::regclass);


--
-- Name: tasks id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks ALTER COLUMN id SET DEFAULT nextval('public.tasks_id_seq'::regclass);


--
-- Name: tool_description_versions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tool_description_versions ALTER COLUMN id SET DEFAULT nextval('public.tool_description_versions_id_seq'::regclass);


--
-- Name: user_overrides id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_overrides ALTER COLUMN id SET DEFAULT nextval('public.user_overrides_id_seq'::regclass);


--
-- Name: critic_verdicts adversary_verdicts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.critic_verdicts
    ADD CONSTRAINT adversary_verdicts_pkey PRIMARY KEY (id);


--
-- Name: agent_runs agent_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_runs
    ADD CONSTRAINT agent_runs_pkey PRIMARY KEY (id);


--
-- Name: agent_sessions agent_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_sessions
    ADD CONSTRAINT agent_sessions_pkey PRIMARY KEY (id);


--
-- Name: bench_runs bench_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bench_runs
    ADD CONSTRAINT bench_runs_pkey PRIMARY KEY (id);


--
-- Name: certifications certifications_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.certifications
    ADD CONSTRAINT certifications_pkey PRIMARY KEY (id);


--
-- Name: chunks chunks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_pkey PRIMARY KEY (id);


--
-- Name: company_state company_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company_state
    ADD CONSTRAINT company_state_pkey PRIMARY KEY (id);


--
-- Name: cooldowns cooldowns_invocation_type_target_type_target_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cooldowns
    ADD CONSTRAINT cooldowns_invocation_type_target_type_target_id_key UNIQUE (invocation_type, target_type, target_id);


--
-- Name: cooldowns cooldowns_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cooldowns
    ADD CONSTRAINT cooldowns_pkey PRIMARY KEY (id);


--
-- Name: cost_tracking cost_tracking_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cost_tracking
    ADD CONSTRAINT cost_tracking_pkey PRIMARY KEY (day);


--
-- Name: datasets datasets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.datasets
    ADD CONSTRAINT datasets_pkey PRIMARY KEY (id);


--
-- Name: deepseek_balance_log deepseek_balance_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deepseek_balance_log
    ADD CONSTRAINT deepseek_balance_log_pkey PRIMARY KEY (id);


--
-- Name: documents documents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_pkey PRIMARY KEY (id);


--
-- Name: events events_event_type_target_type_target_id_dedup_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_event_type_target_type_target_id_dedup_key_key UNIQUE (event_type, target_type, target_id, dedup_key);


--
-- Name: events events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_pkey PRIMARY KEY (id);


--
-- Name: evidence evidence_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evidence
    ADD CONSTRAINT evidence_pkey PRIMARY KEY (id);


--
-- Name: experiment_runs experiment_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.experiment_runs
    ADD CONSTRAINT experiment_runs_pkey PRIMARY KEY (id);


--
-- Name: fetch_cache fetch_cache_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fetch_cache
    ADD CONSTRAINT fetch_cache_pkey PRIMARY KEY (url);


--
-- Name: findings findings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.findings
    ADD CONSTRAINT findings_pkey PRIMARY KEY (id);


--
-- Name: lesson_applications lesson_applications_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lesson_applications
    ADD CONSTRAINT lesson_applications_pkey PRIMARY KEY (id);


--
-- Name: lessons lessons_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lessons
    ADD CONSTRAINT lessons_pkey PRIMARY KEY (id);


--
-- Name: memory_pointers memory_pointers_entity_type_entity_id_zep_message_uuid_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_pointers
    ADD CONSTRAINT memory_pointers_entity_type_entity_id_zep_message_uuid_key UNIQUE (entity_type, entity_id, zep_message_uuid);


--
-- Name: memory_pointers memory_pointers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_pointers
    ADD CONSTRAINT memory_pointers_pkey PRIMARY KEY (id);


--
-- Name: objectives objectives_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.objectives
    ADD CONSTRAINT objectives_pkey PRIMARY KEY (id);


--
-- Name: phase_transitions phase_transitions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phase_transitions
    ADD CONSTRAINT phase_transitions_pkey PRIMARY KEY (id);


--
-- Name: research_inquiries research_inquiries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.research_inquiries
    ADD CONSTRAINT research_inquiries_pkey PRIMARY KEY (id);


--
-- Name: tasks tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_pkey PRIMARY KEY (id);


--
-- Name: claims theses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.claims
    ADD CONSTRAINT theses_pkey PRIMARY KEY (id);


--
-- Name: tool_description_versions tool_description_versions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tool_description_versions
    ADD CONSTRAINT tool_description_versions_pkey PRIMARY KEY (id);


--
-- Name: tool_description_versions tool_description_versions_server_name_tool_name_version_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tool_description_versions
    ADD CONSTRAINT tool_description_versions_server_name_tool_name_version_key UNIQUE (server_name, tool_name, version);


--
-- Name: chunks uq_chunks_doc_ordinal_hash; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT uq_chunks_doc_ordinal_hash UNIQUE (document_id, ordinal, content_hash);


--
-- Name: datasets uq_datasets_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.datasets
    ADD CONSTRAINT uq_datasets_name UNIQUE (name);


--
-- Name: documents uq_documents_canonical; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT uq_documents_canonical UNIQUE (source_kind, canonical_key);


--
-- Name: user_overrides user_overrides_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_overrides
    ADD CONSTRAINT user_overrides_pkey PRIMARY KEY (id);


--
-- Name: idx_agent_runs_open_expectation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_runs_open_expectation ON public.agent_runs USING btree (started_at DESC) WHERE ((expectation IS NOT NULL) AND (outcome IS NULL));


--
-- Name: idx_agent_runs_recent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_runs_recent ON public.agent_runs USING btree (started_at DESC);


--
-- Name: idx_agent_runs_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_runs_session ON public.agent_runs USING btree (session_id, step_order);


--
-- Name: idx_agent_sessions_event; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_sessions_event ON public.agent_sessions USING btree (triggered_by_event_id);


--
-- Name: idx_agent_sessions_handler; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_sessions_handler ON public.agent_sessions USING btree (handler_name, started_at DESC);


--
-- Name: idx_agent_sessions_recent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_sessions_recent ON public.agent_sessions USING btree (started_at DESC);


--
-- Name: idx_bench_runs_recent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bench_runs_recent ON public.bench_runs USING btree (created_at DESC);


--
-- Name: idx_certifications_doc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_certifications_doc ON public.certifications USING btree (document_id, created_at DESC);


--
-- Name: idx_chunks_document; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chunks_document ON public.chunks USING btree (document_id);


--
-- Name: idx_chunks_embedding; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chunks_embedding ON public.chunks USING hnsw (embedding public.vector_cosine_ops) WITH (m='16', ef_construction='64');


--
-- Name: idx_claims_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_claims_active ON public.claims USING btree (status) WHERE (status = 'proposed'::public.claim_status);


--
-- Name: idx_claims_confidence; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_claims_confidence ON public.claims USING btree (confidence DESC) WHERE (status = 'proposed'::public.claim_status);


--
-- Name: idx_cooldowns_until; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cooldowns_until ON public.cooldowns USING btree (cooldown_until);


--
-- Name: idx_datasets_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_datasets_task ON public.datasets USING btree (task);


--
-- Name: idx_documents_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_documents_kind ON public.documents USING btree (kind);


--
-- Name: idx_documents_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_documents_status ON public.documents USING btree (status);


--
-- Name: idx_documents_trust; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_documents_trust ON public.documents USING btree (trust_tier, trust_state);


--
-- Name: idx_ds_balance_recent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ds_balance_recent ON public.deepseek_balance_log USING btree (recorded_at DESC);


--
-- Name: idx_events_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_events_pending ON public.events USING btree (emitted_at) WHERE (status = 'pending'::public.event_status);


--
-- Name: idx_events_recent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_events_recent ON public.events USING btree (emitted_at DESC);


--
-- Name: idx_events_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_events_session ON public.events USING btree (session_id, emitted_at);


--
-- Name: idx_evidence_inquiry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_evidence_inquiry ON public.evidence USING btree (inquiry_id);


--
-- Name: idx_evidence_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_evidence_task ON public.evidence USING btree (task_id, created_at);


--
-- Name: idx_experiments_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_experiments_task ON public.experiment_runs USING btree (task_id);


--
-- Name: idx_fetch_cache_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_fetch_cache_expires ON public.fetch_cache USING btree (expires_at);


--
-- Name: idx_findings_high_signal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_findings_high_signal ON public.findings USING btree (claim_id, relevance_score DESC) WHERE ((audit_verdict = 'pass'::text) AND (relevance_score >= (8)::numeric));


--
-- Name: idx_findings_thesis; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_findings_thesis ON public.findings USING btree (claim_id, created_at DESC);


--
-- Name: idx_inquiries_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_inquiries_task ON public.research_inquiries USING btree (task_id, iteration);


--
-- Name: idx_lesson_applications_lesson_outcome; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lesson_applications_lesson_outcome ON public.lesson_applications USING btree (lesson_id, outcome);


--
-- Name: idx_lesson_applications_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lesson_applications_pending ON public.lesson_applications USING btree (lesson_id) WHERE (outcome IS NULL);


--
-- Name: idx_lessons_active_by_invocation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lessons_active_by_invocation ON public.lessons USING btree (applies_to_invocation, confidence DESC) WHERE (status = ANY (ARRAY['probationary'::public.lesson_status, 'active'::public.lesson_status]));


--
-- Name: idx_lessons_text_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lessons_text_trgm ON public.lessons USING gin (lesson_text public.gin_trgm_ops);


--
-- Name: idx_memory_pointers_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memory_pointers_entity ON public.memory_pointers USING btree (entity_type, entity_id);


--
-- Name: idx_tasks_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tasks_pending ON public.tasks USING btree (priority DESC, created_at) WHERE (status = 'pending'::public.task_status);


--
-- Name: idx_tasks_running; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tasks_running ON public.tasks USING btree (started_at) WHERE (status = 'running'::public.task_status);


--
-- Name: idx_tool_versions_current; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_tool_versions_current ON public.tool_description_versions USING btree (server_name, tool_name) WHERE is_current;


--
-- Name: uq_documents_arxiv; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_documents_arxiv ON public.documents USING btree (arxiv_id) WHERE ((arxiv_id IS NOT NULL) AND (arxiv_id <> ''::text));


--
-- Name: uq_documents_content_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_documents_content_hash ON public.documents USING btree (content_hash) WHERE (content_hash IS NOT NULL);


--
-- Name: uq_documents_doi; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_documents_doi ON public.documents USING btree (doi) WHERE ((doi IS NOT NULL) AND (doi <> ''::text));


--
-- Name: tasks trg_emit_queue_empty; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_emit_queue_empty AFTER UPDATE ON public.tasks FOR EACH ROW EXECUTE FUNCTION public.emit_queue_empty_if_drained();


--
-- Name: tasks trg_emit_task_created; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_emit_task_created AFTER INSERT ON public.tasks FOR EACH ROW EXECUTE FUNCTION public.emit_task_created();


--
-- Name: events trg_notify_event; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_notify_event AFTER INSERT ON public.events FOR EACH ROW EXECUTE FUNCTION public.notify_event();


--
-- Name: agent_runs trg_reflection_on_dissent; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_reflection_on_dissent AFTER UPDATE ON public.agent_runs FOR EACH ROW EXECUTE FUNCTION public.trigger_reflection_on_dissent();


--
-- Name: critic_verdicts adversary_verdicts_thesis_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.critic_verdicts
    ADD CONSTRAINT adversary_verdicts_thesis_id_fkey FOREIGN KEY (thesis_id) REFERENCES public.claims(id);


--
-- Name: agent_runs agent_runs_parent_step_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_runs
    ADD CONSTRAINT agent_runs_parent_step_id_fkey FOREIGN KEY (parent_step_id) REFERENCES public.agent_runs(id);


--
-- Name: agent_runs agent_runs_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_runs
    ADD CONSTRAINT agent_runs_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.agent_sessions(id);


--
-- Name: agent_sessions agent_sessions_triggered_by_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_sessions
    ADD CONSTRAINT agent_sessions_triggered_by_event_id_fkey FOREIGN KEY (triggered_by_event_id) REFERENCES public.events(id);


--
-- Name: certifications certifications_decided_by_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.certifications
    ADD CONSTRAINT certifications_decided_by_run_id_fkey FOREIGN KEY (decided_by_run_id) REFERENCES public.agent_runs(id);


--
-- Name: certifications certifications_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.certifications
    ADD CONSTRAINT certifications_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);


--
-- Name: chunks chunks_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;


--
-- Name: cooldowns cooldowns_set_by_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cooldowns
    ADD CONSTRAINT cooldowns_set_by_run_id_fkey FOREIGN KEY (set_by_run_id) REFERENCES public.agent_runs(id);


--
-- Name: datasets datasets_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.datasets
    ADD CONSTRAINT datasets_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);


--
-- Name: documents documents_certified_by_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_certified_by_run_id_fkey FOREIGN KEY (certified_by_run_id) REFERENCES public.agent_runs(id);


--
-- Name: documents documents_parse_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_parse_run_id_fkey FOREIGN KEY (parse_run_id) REFERENCES public.agent_runs(id);


--
-- Name: events events_consumed_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_consumed_run_id_fkey FOREIGN KEY (consumed_run_id) REFERENCES public.agent_runs(id);


--
-- Name: events events_emitted_by_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_emitted_by_run_id_fkey FOREIGN KEY (emitted_by_run_id) REFERENCES public.agent_runs(id);


--
-- Name: events events_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.agent_sessions(id);


--
-- Name: evidence evidence_extract_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evidence
    ADD CONSTRAINT evidence_extract_run_id_fkey FOREIGN KEY (extract_run_id) REFERENCES public.agent_runs(id);


--
-- Name: evidence evidence_inquiry_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evidence
    ADD CONSTRAINT evidence_inquiry_id_fkey FOREIGN KEY (inquiry_id) REFERENCES public.research_inquiries(id);


--
-- Name: evidence evidence_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evidence
    ADD CONSTRAINT evidence_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id);


--
-- Name: experiment_runs experiment_runs_inquiry_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.experiment_runs
    ADD CONSTRAINT experiment_runs_inquiry_id_fkey FOREIGN KEY (inquiry_id) REFERENCES public.research_inquiries(id);


--
-- Name: experiment_runs experiment_runs_interpret_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.experiment_runs
    ADD CONSTRAINT experiment_runs_interpret_run_id_fkey FOREIGN KEY (interpret_run_id) REFERENCES public.agent_runs(id);


--
-- Name: experiment_runs experiment_runs_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.experiment_runs
    ADD CONSTRAINT experiment_runs_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id);


--
-- Name: findings findings_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.findings
    ADD CONSTRAINT findings_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id);


--
-- Name: findings findings_thesis_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.findings
    ADD CONSTRAINT findings_thesis_id_fkey FOREIGN KEY (claim_id) REFERENCES public.claims(id);


--
-- Name: lesson_applications lesson_applications_agent_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lesson_applications
    ADD CONSTRAINT lesson_applications_agent_run_id_fkey FOREIGN KEY (agent_run_id) REFERENCES public.agent_runs(id);


--
-- Name: lesson_applications lesson_applications_lesson_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lesson_applications
    ADD CONSTRAINT lesson_applications_lesson_id_fkey FOREIGN KEY (lesson_id) REFERENCES public.lessons(id);


--
-- Name: lesson_applications lesson_applications_outcome_judged_by_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lesson_applications
    ADD CONSTRAINT lesson_applications_outcome_judged_by_run_id_fkey FOREIGN KEY (outcome_judged_by_run_id) REFERENCES public.agent_runs(id);


--
-- Name: lessons lessons_derived_from_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lessons
    ADD CONSTRAINT lessons_derived_from_run_id_fkey FOREIGN KEY (derived_from_run_id) REFERENCES public.agent_runs(id);


--
-- Name: lessons lessons_superseded_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lessons
    ADD CONSTRAINT lessons_superseded_by_fkey FOREIGN KEY (superseded_by) REFERENCES public.lessons(id);


--
-- Name: lessons lessons_supersedes_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lessons
    ADD CONSTRAINT lessons_supersedes_fkey FOREIGN KEY (supersedes) REFERENCES public.lessons(id);


--
-- Name: objectives objectives_thesis_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.objectives
    ADD CONSTRAINT objectives_thesis_id_fkey FOREIGN KEY (claim_id) REFERENCES public.claims(id);


--
-- Name: phase_transitions phase_transitions_proposed_by_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phase_transitions
    ADD CONSTRAINT phase_transitions_proposed_by_run_id_fkey FOREIGN KEY (proposed_by_run_id) REFERENCES public.agent_runs(id);


--
-- Name: research_inquiries research_inquiries_plan_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.research_inquiries
    ADD CONSTRAINT research_inquiries_plan_run_id_fkey FOREIGN KEY (plan_run_id) REFERENCES public.agent_runs(id);


--
-- Name: research_inquiries research_inquiries_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.research_inquiries
    ADD CONSTRAINT research_inquiries_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id);


--
-- Name: tasks tasks_objective_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_objective_id_fkey FOREIGN KEY (objective_id) REFERENCES public.objectives(id);


--
-- Name: tasks tasks_thesis_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_thesis_id_fkey FOREIGN KEY (claim_id) REFERENCES public.claims(id);


--
-- Name: claims theses_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.claims
    ADD CONSTRAINT theses_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.claims(id);


--
-- Name: tool_description_versions tool_description_versions_derived_from_lesson_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tool_description_versions
    ADD CONSTRAINT tool_description_versions_derived_from_lesson_id_fkey FOREIGN KEY (derived_from_lesson_id) REFERENCES public.lessons(id);


--
-- PostgreSQL database dump complete
--


