use std::path::PathBuf;
use std::sync::Arc;
use parking_lot::Mutex;
use rusqlite::{params, Connection};
use crate::models::*;

#[derive(Clone)]
pub struct DbManager {
    _db_path: PathBuf,
    _logs_db_path: PathBuf,
    conn: Arc<Mutex<Connection>>,
}

impl DbManager {
    pub fn new(db_path: &str, logs_db_path: &str) -> Result<Self, rusqlite::Error> {
        let p = PathBuf::from(db_path);
        let lp = PathBuf::from(logs_db_path);
        let conn = Connection::open(&p)?;
        // Enable WAL mode and busy timeout for concurrent safety
        let _ = conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;");
        Ok(Self {
            _db_path: p,
            _logs_db_path: lp,
            conn: Arc::new(Mutex::new(conn)),
        })
    }

    pub fn get_all_rules(&self) -> Result<Vec<ForwardRuleModel>, String> {
        let conn = self.conn.lock();
        let mut stmt = conn.prepare(
            "SELECT id, session_id, source_chat_id, source_chat_name, target_chat_id, target_chat_name, \
             routing_type, forward_mode, is_active, is_paused, priority, message_category, \
             use_intermediate, intermediate_channel_id, intermediate_channel_name, \
             fallback_mode, fallback_enabled, detection_criteria, link_policy, \
             custom_header, custom_footer, split_long_caption, created_at, updated_at \
             FROM forward_rules ORDER BY priority DESC, created_at ASC"
        ).map_err(|e| e.to_string())?;

        let rows = stmt.query_map([], |row| {
            let det_raw: String = row.get(17).unwrap_or_else(|_| "{}".to_string());
            let det_val: serde_json::Value = serde_json::from_str(&det_raw).unwrap_or_else(|_| serde_json::json!({}));
            Ok(ForwardRuleModel {
                id: row.get(0)?,
                session_id: row.get(1)?,
                source_chat_id: row.get(2)?,
                source_chat_name: row.get(3)?,
                target_chat_id: row.get(4)?,
                target_chat_name: row.get(5)?,
                routing_type: row.get(6)?,
                forward_mode: row.get(7)?,
                is_active: row.get::<_, i32>(8)? != 0,
                is_paused: row.get::<_, i32>(9)? != 0,
                priority: row.get(10)?,
                message_category: row.get(11)?,
                use_intermediate: row.get::<_, i32>(12)? != 0,
                intermediate_channel_id: row.get(13)?,
                intermediate_channel_name: row.get(14)?,
                fallback_mode: row.get(15)?,
                fallback_enabled: row.get::<_, i32>(16)? != 0,
                detection_criteria: det_val,
                link_policy: row.get(18).unwrap_or_else(|_| "PRESERVE_ALL".to_string()),
                custom_header: row.get(19).unwrap_or_default(),
                custom_footer: row.get(20).unwrap_or_default(),
                split_long_caption: row.get::<_, i32>(21).unwrap_or(1) != 0,
                created_at: row.get(22)?,
                updated_at: row.get(23)?,
            })
        }).map_err(|e| e.to_string())?;

        let mut list = Vec::new();
        for r in rows {
            list.push(r.map_err(|e| e.to_string())?);
        }
        Ok(list)
    }

    pub fn get_rule(&self, id: &str) -> Result<Option<ForwardRuleModel>, String> {
        let conn = self.conn.lock();
        let mut stmt = conn.prepare(
            "SELECT id, session_id, source_chat_id, source_chat_name, target_chat_id, target_chat_name, \
             routing_type, forward_mode, is_active, is_paused, priority, message_category, \
             use_intermediate, intermediate_channel_id, intermediate_channel_name, \
             fallback_mode, fallback_enabled, detection_criteria, link_policy, \
             custom_header, custom_footer, split_long_caption, created_at, updated_at \
             FROM forward_rules WHERE id = ?1"
        ).map_err(|e| e.to_string())?;

        let mut rows = stmt.query_map(params![id], |row| {
            let det_raw: String = row.get(17).unwrap_or_else(|_| "{}".to_string());
            let det_val: serde_json::Value = serde_json::from_str(&det_raw).unwrap_or_else(|_| serde_json::json!({}));
            Ok(ForwardRuleModel {
                id: row.get(0)?,
                session_id: row.get(1)?,
                source_chat_id: row.get(2)?,
                source_chat_name: row.get(3)?,
                target_chat_id: row.get(4)?,
                target_chat_name: row.get(5)?,
                routing_type: row.get(6)?,
                forward_mode: row.get(7)?,
                is_active: row.get::<_, i32>(8)? != 0,
                is_paused: row.get::<_, i32>(9)? != 0,
                priority: row.get(10)?,
                message_category: row.get(11)?,
                use_intermediate: row.get::<_, i32>(12)? != 0,
                intermediate_channel_id: row.get(13)?,
                intermediate_channel_name: row.get(14)?,
                fallback_mode: row.get(15)?,
                fallback_enabled: row.get::<_, i32>(16)? != 0,
                detection_criteria: det_val,
                link_policy: row.get(18).unwrap_or_else(|_| "PRESERVE_ALL".to_string()),
                custom_header: row.get(19).unwrap_or_default(),
                custom_footer: row.get(20).unwrap_or_default(),
                split_long_caption: row.get::<_, i32>(21).unwrap_or(1) != 0,
                created_at: row.get(22)?,
                updated_at: row.get(23)?,
            })
        }).map_err(|e| e.to_string())?;

        if let Some(r) = rows.next() {
            Ok(Some(r.map_err(|e| e.to_string())?))
        } else {
            Ok(None)
        }
    }

    pub fn save_rule(&self, req: &SaveRuleRequest) -> Result<ForwardRuleModel, String> {
        let conn = self.conn.lock();
        let now = chrono::Utc::now().timestamp();
        let rule_id = req.id.clone().unwrap_or_else(|| format!("rule_{}", uuid::Uuid::new_v4().simple()));
        let session_id = req.session_id.clone().unwrap_or_else(|| {
            // Find first active session
            let mut s = conn.prepare("SELECT id FROM sessions WHERE is_active=1 LIMIT 1").unwrap();
            let first_id: Result<String, _> = s.query_row([], |r| r.get(0));
            first_id.unwrap_or_else(|_| "default".to_string())
        });

        let src_id = req.source_chat_id.clone();
        let src_name = req.source_chat_name.clone().unwrap_or_default();
        let tgt_id = req.target_chat_id.clone();
        let tgt_name = req.target_chat_name.clone().unwrap_or_default();
        let r_type = req.routing_type.clone().unwrap_or_else(|| "CHANNEL_TO_CHANNEL".to_string());
        let f_mode = req.forward_mode.clone().unwrap_or_else(|| "COPY_MESSAGE".to_string());
        let is_act = if req.is_active.unwrap_or(true) { 1 } else { 0 };
        let is_pau = if req.is_paused.unwrap_or(false) { 1 } else { 0 };
        let prio = req.priority.unwrap_or(10);
        let cat = req.message_category.clone().unwrap_or_else(|| "ALL".to_string());
        let use_mid = if req.use_intermediate.unwrap_or(false) { 1 } else { 0 };
        let mid_id = req.intermediate_channel_id.clone().unwrap_or_default();
        let mid_name = req.intermediate_channel_name.clone().unwrap_or_default();
        let fb_mode = req.fallback_mode.clone().unwrap_or_else(|| "COPY_MESSAGE".to_string());
        let fb_en = if req.fallback_enabled.unwrap_or(true) { 1 } else { 0 };
        let det_str = match &req.detection_criteria {
            Some(v) => serde_json::to_string(v).unwrap_or_else(|_| "{}".to_string()),
            None => "{}".to_string(),
        };
        let l_pol = req.link_policy.clone().unwrap_or_else(|| "PRESERVE_ALL".to_string());
        let chdr = req.custom_header.clone().unwrap_or_default();
        let cftr = req.custom_footer.clone().unwrap_or_default();
        let split_c = if req.split_long_caption.unwrap_or(true) { 1 } else { 0 };

        conn.execute(
            "INSERT INTO forward_rules (
                id, session_id, source_chat_id, source_chat_name, target_chat_id, target_chat_name,
                routing_type, forward_mode, is_active, is_paused, priority, message_category,
                use_intermediate, intermediate_channel_id, intermediate_channel_name,
                fallback_mode, fallback_enabled, detection_criteria, link_policy,
                custom_header, custom_footer, split_long_caption, created_at, updated_at
            ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19, ?20, ?21, ?22, ?23, ?24)
            ON CONFLICT(id) DO UPDATE SET
                source_chat_id = excluded.source_chat_id,
                source_chat_name = excluded.source_chat_name,
                target_chat_id = excluded.target_chat_id,
                target_chat_name = excluded.target_chat_name,
                routing_type = excluded.routing_type,
                forward_mode = excluded.forward_mode,
                is_active = excluded.is_active,
                is_paused = excluded.is_paused,
                priority = excluded.priority,
                message_category = excluded.message_category,
                use_intermediate = excluded.use_intermediate,
                intermediate_channel_id = excluded.intermediate_channel_id,
                intermediate_channel_name = excluded.intermediate_channel_name,
                fallback_mode = excluded.fallback_mode,
                fallback_enabled = excluded.fallback_enabled,
                detection_criteria = excluded.detection_criteria,
                link_policy = excluded.link_policy,
                custom_header = excluded.custom_header,
                custom_footer = excluded.custom_footer,
                split_long_caption = excluded.split_long_caption,
                updated_at = excluded.updated_at",
            params![
                rule_id, session_id, src_id, src_name, tgt_id, tgt_name,
                r_type, f_mode, is_act, is_pau, prio, cat,
                use_mid, mid_id, mid_name, fb_mode, fb_en, det_str, l_pol,
                chdr, cftr, split_c, now, now
            ],
        ).map_err(|e| e.to_string())?;

        drop(conn);
        self.get_rule(&rule_id)?.ok_or_else(|| "Failed to fetch saved rule".to_string())
    }

    pub fn delete_rule(&self, id: &str) -> Result<bool, String> {
        let conn = self.conn.lock();
        let affected = conn.execute("DELETE FROM forward_rules WHERE id = ?1", params![id])
            .map_err(|e| e.to_string())?;
        Ok(affected > 0)
    }

    pub fn toggle_rule(&self, id: &str) -> Result<ForwardRuleModel, String> {
        let conn = self.conn.lock();
        let now = chrono::Utc::now().timestamp();
        conn.execute(
            "UPDATE forward_rules SET is_active = (1 - is_active), updated_at = ?2 WHERE id = ?1",
            params![id, now],
        ).map_err(|e| e.to_string())?;
        drop(conn);
        self.get_rule(id)?.ok_or_else(|| "Rule not found".to_string())
    }

    pub fn quick_set_route_path(&self, id: &str, req: &RoutePathQuickSetRequest) -> Result<ForwardRuleModel, String> {
        let mut cur = self.get_rule(id)?.ok_or_else(|| "Rule not found".to_string())?;
        let now = chrono::Utc::now().timestamp();

        match req.path_type.as_str() {
            "route2_vip_hop" => {
                cur.use_intermediate = true;
                cur.message_category = "VIP_ONLY".to_string();
                cur.forward_mode = "CUSTOM_HEADER_COPY".to_string();
                if let Some(ref mid_id) = req.intermediate_channel_id {
                    cur.intermediate_channel_id = mid_id.clone();
                }
                if let Some(ref mid_name) = req.intermediate_channel_name {
                    cur.intermediate_channel_name = mid_name.clone();
                }
            }
            "route3_native" => {
                cur.use_intermediate = false;
                cur.message_category = "ALL".to_string();
                cur.forward_mode = "DIRECT_FORWARD".to_string();
            }
            _ => { // "route1_direct"
                cur.use_intermediate = false;
                cur.message_category = "ALL".to_string();
                cur.forward_mode = "COPY_MESSAGE".to_string();
            }
        }

        let mut criteria = cur.detection_criteria.as_object().cloned().unwrap_or_default();
        if let Some(ref m_mode) = req.match_mode {
            criteria.insert("match_mode".to_string(), serde_json::Value::String(m_mode.clone()));
        }
        if let Some(ref kws) = req.keywords {
            let kw_json: Vec<serde_json::Value> = kws.iter().map(|k| serde_json::Value::String(k.clone())).collect();
            criteria.insert("text_contains".to_string(), serde_json::Value::Array(kw_json.clone()));
            criteria.insert("keywords".to_string(), serde_json::Value::Array(kw_json));
        }
        if let Some(ref rx) = req.regex_pattern {
            criteria.insert("regex_pattern".to_string(), serde_json::Value::String(rx.clone()));
            criteria.insert("text_regex".to_string(), serde_json::Value::String(rx.clone()));
        }
        let det_str = serde_json::to_string(&criteria).unwrap_or_else(|_| "{}".to_string());

        if let Some(ref h) = req.custom_header {
            cur.custom_header = h.clone();
        }

        let conn = self.conn.lock();
        conn.execute(
            "UPDATE forward_rules SET
                use_intermediate = ?1,
                intermediate_channel_id = ?2,
                intermediate_channel_name = ?3,
                message_category = ?4,
                forward_mode = ?5,
                detection_criteria = ?6,
                custom_header = ?7,
                updated_at = ?8
             WHERE id = ?9",
            params![
                if cur.use_intermediate { 1 } else { 0 },
                cur.intermediate_channel_id,
                cur.intermediate_channel_name,
                cur.message_category,
                cur.forward_mode,
                det_str,
                cur.custom_header,
                now,
                id
            ],
        ).map_err(|e| e.to_string())?;

        drop(conn);
        self.get_rule(id)?.ok_or_else(|| "Rule not found".to_string())
    }

    pub fn get_sessions(&self) -> Result<Vec<SessionModel>, String> {
        let conn = self.conn.lock();
        let mut stmt = conn.prepare(
            "SELECT id, phone_number, user_id, username, first_name, is_active, is_authorized, created_at, updated_at \
             FROM sessions ORDER BY created_at DESC"
        ).map_err(|e| e.to_string())?;

        let rows = stmt.query_map([], |row| {
            Ok(SessionModel {
                id: row.get(0)?,
                phone_number: row.get(1)?,
                user_id: row.get(2)?,
                username: row.get(3)?,
                first_name: row.get(4)?,
                is_active: row.get::<_, i32>(5)? != 0,
                is_authorized: row.get::<_, i32>(6)? != 0,
                created_at: row.get(7)?,
                updated_at: row.get(8)?,
            })
        }).map_err(|e| e.to_string())?;

        let mut list = Vec::new();
        for r in rows {
            list.push(r.map_err(|e| e.to_string())?);
        }
        Ok(list)
    }

    pub fn get_stats(&self) -> Result<StatsResponse, String> {
        let conn = self.conn.lock();
        let total_rules: i64 = conn.query_row("SELECT count(*) FROM forward_rules", [], |r| r.get(0)).unwrap_or(0);
        let active_rules: i64 = conn.query_row("SELECT count(*) FROM forward_rules WHERE is_active=1", [], |r| r.get(0)).unwrap_or(0);
        let total_sessions: i64 = conn.query_row("SELECT count(*) FROM sessions", [], |r| r.get(0)).unwrap_or(0);
        let active_sessions: i64 = conn.query_row("SELECT count(*) FROM sessions WHERE is_active=1", [], |r| r.get(0)).unwrap_or(0);
        let total_fwd: i64 = conn.query_row("SELECT coalesce(sum(forwarded), 0) FROM rule_stats", [], |r| r.get(0)).unwrap_or(0);
        let total_err: i64 = conn.query_row("SELECT coalesce(sum(errors), 0) FROM rule_stats", [], |r| r.get(0)).unwrap_or(0);
        let q_pending: i64 = conn.query_row("SELECT count(*) FROM delivery_jobs WHERE status='PENDING'", [], |r| r.get(0)).unwrap_or(0);
        let q_failed: i64 = conn.query_row("SELECT count(*) FROM delivery_jobs WHERE status='FAILED'", [], |r| r.get(0)).unwrap_or(0);

        // Check if port 6001 and 6002 are responding
        let atf_core_online = std::net::TcpStream::connect("127.0.0.1:6001").is_ok();
        let atf_logger_online = std::net::TcpStream::connect("127.0.0.1:6002").is_ok();

        Ok(StatsResponse {
            total_rules,
            active_rules,
            total_sessions,
            active_sessions,
            total_forwarded_24h: total_fwd,
            total_errors_24h: total_err,
            queue_jobs_pending: q_pending,
            queue_jobs_failed: q_failed,
            atf_core_online,
            atf_logger_online,
        })
    }

    pub fn get_recent_logs(&self, limit: usize) -> Result<Vec<LogItemModel>, String> {
        let conn = self.conn.lock();
        let mut stmt = conn.prepare(
            "SELECT id, ts, category, error_name, severity, detail, rule_id, chat_id \
             FROM error_log ORDER BY ts DESC LIMIT ?1"
        ).map_err(|e| e.to_string())?;

        let rows = stmt.query_map(params![limit as i64], |row| {
            Ok(LogItemModel {
                id: row.get(0)?,
                ts: row.get(1)?,
                category: row.get(2)?,
                error_name: row.get(3)?,
                severity: row.get(4)?,
                detail: row.get(5)?,
                rule_id: row.get(6)?,
                chat_id: row.get(7)?,
            })
        }).map_err(|e| e.to_string())?;

        let mut list = Vec::new();
        for r in rows {
            list.push(r.map_err(|e| e.to_string())?);
        }
        Ok(list)
    }
}
