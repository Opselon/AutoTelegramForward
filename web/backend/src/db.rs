use std::path::PathBuf;
use std::sync::Arc;
use parking_lot::Mutex;
use rusqlite::{params, Connection};
use crate::models::*;

#[derive(Clone)]
pub struct DbManager {
    db_path: PathBuf,
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
            db_path: p,
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
             custom_header, custom_footer, split_long_caption, filter_rule_id, ai_config_id, \
             remove_links, delay_seconds, rate_limit_per_minute, max_retries, replacements, \
             domain_allowlist, domain_blocklist, link_rewrite_map, created_at, updated_at \
             FROM forward_rules ORDER BY priority DESC, created_at ASC"
        ).map_err(|e| e.to_string())?;

        let rows = stmt.query_map([], |row| {
            let det_raw: String = row.get(17).unwrap_or_else(|_| "{}".to_string());
            let det_val: serde_json::Value = serde_json::from_str(&det_raw).unwrap_or_else(|_| serde_json::json!({}));
            
            let rep_raw: String = row.get(28).unwrap_or_else(|_| "{}".to_string());
            let rep_val: serde_json::Value = serde_json::from_str(&rep_raw).unwrap_or_else(|_| serde_json::json!({}));
            
            let allow_raw: String = row.get(29).unwrap_or_else(|_| "[]".to_string());
            let allow_val: Vec<String> = serde_json::from_str(&allow_raw).unwrap_or_default();
            
            let block_raw: String = row.get(30).unwrap_or_else(|_| "[]".to_string());
            let block_val: Vec<String> = serde_json::from_str(&block_raw).unwrap_or_default();

            let rw_raw: String = row.get(31).unwrap_or_else(|_| "{}".to_string());
            let rw_val: serde_json::Value = serde_json::from_str(&rw_raw).unwrap_or_else(|_| serde_json::json!({}));

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
                filter_rule_id: row.get(22).ok(),
                ai_config_id: row.get(23).ok(),
                remove_links: row.get::<_, i32>(24).unwrap_or(0) != 0,
                delay_seconds: row.get(25).unwrap_or(0.0),
                rate_limit_per_minute: row.get(26).unwrap_or(0),
                max_retries: row.get(27).unwrap_or(3),
                replacements: rep_val,
                domain_allowlist: allow_val,
                domain_blocklist: block_val,
                link_rewrite_map: rw_val,
                created_at: row.get(32)?,
                updated_at: row.get(33)?,
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
             custom_header, custom_footer, split_long_caption, filter_rule_id, ai_config_id, \
             remove_links, delay_seconds, rate_limit_per_minute, max_retries, replacements, \
             domain_allowlist, domain_blocklist, link_rewrite_map, created_at, updated_at \
             FROM forward_rules WHERE id = ?1"
        ).map_err(|e| e.to_string())?;

        let mut rows = stmt.query_map(params![id], |row| {
            let det_raw: String = row.get(17).unwrap_or_else(|_| "{}".to_string());
            let det_val: serde_json::Value = serde_json::from_str(&det_raw).unwrap_or_else(|_| serde_json::json!({}));
            
            let rep_raw: String = row.get(28).unwrap_or_else(|_| "{}".to_string());
            let rep_val: serde_json::Value = serde_json::from_str(&rep_raw).unwrap_or_else(|_| serde_json::json!({}));
            
            let allow_raw: String = row.get(29).unwrap_or_else(|_| "[]".to_string());
            let allow_val: Vec<String> = serde_json::from_str(&allow_raw).unwrap_or_default();
            
            let block_raw: String = row.get(30).unwrap_or_else(|_| "[]".to_string());
            let block_val: Vec<String> = serde_json::from_str(&block_raw).unwrap_or_default();

            let rw_raw: String = row.get(31).unwrap_or_else(|_| "{}".to_string());
            let rw_val: serde_json::Value = serde_json::from_str(&rw_raw).unwrap_or_else(|_| serde_json::json!({}));

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
                filter_rule_id: row.get(22).ok(),
                ai_config_id: row.get(23).ok(),
                remove_links: row.get::<_, i32>(24).unwrap_or(0) != 0,
                delay_seconds: row.get(25).unwrap_or(0.0),
                rate_limit_per_minute: row.get(26).unwrap_or(0),
                max_retries: row.get(27).unwrap_or(3),
                replacements: rep_val,
                domain_allowlist: allow_val,
                domain_blocklist: block_val,
                link_rewrite_map: rw_val,
                created_at: row.get(32)?,
                updated_at: row.get(33)?,
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
        let id = req.id.clone().unwrap_or_else(|| uuid::Uuid::new_v4().to_string());

        // Get default session if not provided
        let session_id = if let Some(ref s) = req.session_id {
            s.clone()
        } else {
            let def_session: Result<String, _> = conn.query_row(
                "SELECT id FROM sessions WHERE is_active = 1 LIMIT 1",
                [],
                |r| r.get(0),
            );
            def_session.unwrap_or_else(|_| "default".to_string())
        };

        let det_str = match &req.detection_criteria {
            Some(v) => serde_json::to_string(v).unwrap_or_else(|_| "{}".to_string()),
            None => "{}".to_string(),
        };

        let rep_str = match &req.replacements {
            Some(v) => serde_json::to_string(v).unwrap_or_else(|_| "{}".to_string()),
            None => "{}".to_string(),
        };

        let allow_str = match &req.domain_allowlist {
            Some(v) => serde_json::to_string(v).unwrap_or_else(|_| "[]".to_string()),
            None => "[]".to_string(),
        };

        let block_str = match &req.domain_blocklist {
            Some(v) => serde_json::to_string(v).unwrap_or_else(|_| "[]".to_string()),
            None => "[]".to_string(),
        };

        let rw_str = match &req.link_rewrite_map {
            Some(v) => serde_json::to_string(v).unwrap_or_else(|_| "{}".to_string()),
            None => "{}".to_string(),
        };

        conn.execute(
            "INSERT INTO forward_rules (
                id, session_id, source_chat_id, source_chat_name, target_chat_id, target_chat_name,
                routing_type, forward_mode, is_active, is_paused, priority, message_category,
                use_intermediate, intermediate_channel_id, intermediate_channel_name,
                fallback_mode, fallback_enabled, detection_criteria, link_policy,
                custom_header, custom_footer, split_long_caption, filter_rule_id, ai_config_id,
                remove_links, delay_seconds, rate_limit_per_minute, max_retries, replacements,
                domain_allowlist, domain_blocklist, link_rewrite_map, created_at, updated_at
            ) VALUES (
                ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19, ?20,
                ?21, ?22, ?23, ?24, ?25, ?26, ?27, ?28, ?29, ?30, ?31, ?32, ?33, ?34
            ) ON CONFLICT(id) DO UPDATE SET
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
                filter_rule_id = excluded.filter_rule_id,
                ai_config_id = excluded.ai_config_id,
                remove_links = excluded.remove_links,
                delay_seconds = excluded.delay_seconds,
                rate_limit_per_minute = excluded.rate_limit_per_minute,
                max_retries = excluded.max_retries,
                replacements = excluded.replacements,
                domain_allowlist = excluded.domain_allowlist,
                domain_blocklist = excluded.domain_blocklist,
                link_rewrite_map = excluded.link_rewrite_map,
                updated_at = excluded.updated_at",
            params![
                id,
                session_id,
                req.source_chat_id,
                req.source_chat_name.clone().unwrap_or_default(),
                req.target_chat_id,
                req.target_chat_name.clone().unwrap_or_default(),
                req.routing_type.clone().unwrap_or_else(|| "CUSTOM".to_string()),
                req.forward_mode.clone().unwrap_or_else(|| "COPY_MESSAGE".to_string()),
                if req.is_active.unwrap_or(true) { 1 } else { 0 },
                if req.is_paused.unwrap_or(false) { 1 } else { 0 },
                req.priority.unwrap_or(10),
                req.message_category.clone().unwrap_or_else(|| "ALL".to_string()),
                if req.use_intermediate.unwrap_or(false) { 1 } else { 0 },
                req.intermediate_channel_id.clone().unwrap_or_default(),
                req.intermediate_channel_name.clone().unwrap_or_default(),
                req.fallback_mode.clone().unwrap_or_else(|| "COPY_MESSAGE".to_string()),
                if req.fallback_enabled.unwrap_or(true) { 1 } else { 0 },
                det_str,
                req.link_policy.clone().unwrap_or_else(|| "PRESERVE_ALL".to_string()),
                req.custom_header.clone().unwrap_or_default(),
                req.custom_footer.clone().unwrap_or_default(),
                if req.split_long_caption.unwrap_or(true) { 1 } else { 0 },
                req.filter_rule_id,
                req.ai_config_id,
                if req.remove_links.unwrap_or(false) { 1 } else { 0 },
                req.delay_seconds.unwrap_or(0.0),
                req.rate_limit_per_minute.unwrap_or(0),
                req.max_retries.unwrap_or(3),
                rep_str,
                allow_str,
                block_str,
                rw_str,
                now,
                now
            ],
        ).map_err(|e| e.to_string())?;

        drop(conn);
        self.get_rule(&id)?.ok_or_else(|| "Failed to reload saved rule".to_string())
    }

    pub fn delete_rule(&self, id: &str) -> Result<bool, String> {
        let conn = self.conn.lock();
        let count = conn.execute("DELETE FROM forward_rules WHERE id = ?1", params![id])
            .map_err(|e| e.to_string())?;
        Ok(count > 0)
    }

    pub fn toggle_rule(&self, id: &str) -> Result<ForwardRuleModel, String> {
        let conn = self.conn.lock();
        let now = chrono::Utc::now().timestamp();
        conn.execute(
            "UPDATE forward_rules SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END, updated_at = ?2 WHERE id = ?1",
            params![id, now],
        ).map_err(|e| e.to_string())?;

        drop(conn);
        self.get_rule(id)?.ok_or_else(|| "Rule not found".to_string())
    }

    pub fn quick_set_route_path(&self, id: &str, req: &RoutePathQuickSetRequest) -> Result<ForwardRuleModel, String> {
        let mut cur = self.get_rule(id)?.ok_or_else(|| "Rule not found".to_string())?;

        match req.path_type.as_str() {
            "route1_direct" => {
                cur.use_intermediate = false;
                cur.forward_mode = "COPY_MESSAGE".to_string();
                cur.message_category = "ALL".to_string();
            }
            "route2_vip_hop" => {
                cur.use_intermediate = true;
                cur.forward_mode = "CUSTOM_HEADER_COPY".to_string();
                cur.message_category = "VIP_ONLY".to_string();
                if let Some(ref ch_id) = req.intermediate_channel_id {
                    cur.intermediate_channel_id = ch_id.clone();
                }
                if let Some(ref ch_name) = req.intermediate_channel_name {
                    cur.intermediate_channel_name = ch_name.clone();
                }
            }
            "route3_native" => {
                cur.use_intermediate = false;
                cur.forward_mode = "DIRECT_FORWARD".to_string();
                cur.message_category = "ALL".to_string();
            }
            _ => return Err("Invalid path_type. Must be route1_direct, route2_vip_hop, or route3_native".to_string()),
        }

        if let Some(ref mm) = req.match_mode {
            let mut crit = cur.detection_criteria.as_object().cloned().unwrap_or_default();
            crit.insert("match_mode".to_string(), serde_json::Value::String(mm.clone()));
            cur.detection_criteria = serde_json::Value::Object(crit);
        }

        if let Some(ref kw) = req.keywords {
            let mut crit = cur.detection_criteria.as_object().cloned().unwrap_or_default();
            let kw_json: Vec<serde_json::Value> = kw.iter().map(|s| serde_json::Value::String(s.clone())).collect();
            crit.insert("text_contains".to_string(), serde_json::Value::Array(kw_json));
            cur.detection_criteria = serde_json::Value::Object(crit);
        }

        if let Some(ref rx) = req.regex_pattern {
            let mut crit = cur.detection_criteria.as_object().cloned().unwrap_or_default();
            crit.insert("regex_pattern".to_string(), serde_json::Value::String(rx.clone()));
            cur.detection_criteria = serde_json::Value::Object(crit);
        }

        if let Some(ref h) = req.custom_header {
            cur.custom_header = h.clone();
        }

        let conn = self.conn.lock();
        let now = chrono::Utc::now().timestamp();
        let det_str = serde_json::to_string(&cur.detection_criteria).unwrap_or_else(|_| "{}".to_string());

        conn.execute(
            "UPDATE forward_rules SET \
             use_intermediate = ?1, intermediate_channel_id = ?2, intermediate_channel_name = ?3, \
             message_category = ?4, forward_mode = ?5, detection_criteria = ?6, custom_header = ?7, \
             updated_at = ?8 WHERE id = ?9",
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
            "SELECT id, phone_number, user_id, username, first_name, is_active, is_authorized, proxy, created_at, updated_at \
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
                proxy: row.get(7).unwrap_or_default(),
                created_at: row.get(8)?,
                updated_at: row.get(9)?,
            })
        }).map_err(|e| e.to_string())?;

        let mut list = Vec::new();
        for r in rows {
            list.push(r.map_err(|e| e.to_string())?);
        }
        Ok(list)
    }

    // AI Configs
    pub fn get_all_ai_configs(&self) -> Result<Vec<AIConfigModel>, String> {
        let conn = self.conn.lock();
        let mut stmt = conn.prepare(
            "SELECT id, name, provider, model, api_key_encrypted, base_url, system_prompt, \
             user_prompt_template, temperature, is_enabled, target_language FROM ai_configs ORDER BY name ASC"
        ).map_err(|e| e.to_string())?;

        let rows = stmt.query_map([], |row| {
            let raw_key: String = row.get(4).unwrap_or_default();
            let masked_key = if raw_key.len() > 8 {
                format!("{}...{}", &raw_key[..4], &raw_key[raw_key.len()-4..])
            } else if !raw_key.is_empty() {
                "********".to_string()
            } else {
                "".to_string()
            };

            Ok(AIConfigModel {
                id: row.get(0)?,
                name: row.get(1)?,
                provider: row.get(2)?,
                model: row.get(3)?,
                api_key_masked: masked_key,
                base_url: row.get(5).unwrap_or_default(),
                system_prompt: row.get(6).unwrap_or_default(),
                user_prompt_template: row.get(7).unwrap_or_default(),
                temperature: row.get(8).unwrap_or(0.7),
                is_enabled: row.get::<_, i32>(9).unwrap_or(1) != 0,
                target_language: row.get(10).unwrap_or_else(|_| "en".to_string()),
            })
        }).map_err(|e| e.to_string())?;

        let mut list = Vec::new();
        for r in rows {
            list.push(r.map_err(|e| e.to_string())?);
        }
        Ok(list)
    }

    pub fn save_ai_config(&self, req: &SaveAIConfigRequest) -> Result<AIConfigModel, String> {
        let conn = self.conn.lock();
        let id = req.id.clone().unwrap_or_else(|| uuid::Uuid::new_v4().to_string());

        let raw_key = req.api_key.clone().unwrap_or_default();
        let temp = req.temperature.unwrap_or(0.7);
        let enabled = if req.is_enabled.unwrap_or(true) { 1 } else { 0 };
        let target_lang = req.target_language.clone().unwrap_or_else(|| "fa".to_string());

        conn.execute(
            "INSERT INTO ai_configs (
                id, name, provider, model, api_key_encrypted, base_url, system_prompt,
                user_prompt_template, temperature, is_enabled, target_language
            ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                provider = excluded.provider,
                model = excluded.model,
                api_key_encrypted = CASE WHEN excluded.api_key_encrypted != '' THEN excluded.api_key_encrypted ELSE ai_configs.api_key_encrypted END,
                base_url = excluded.base_url,
                system_prompt = excluded.system_prompt,
                user_prompt_template = excluded.user_prompt_template,
                temperature = excluded.temperature,
                is_enabled = excluded.is_enabled,
                target_language = excluded.target_language",
            params![
                id,
                req.name,
                req.provider,
                req.model,
                raw_key,
                req.base_url.clone().unwrap_or_default(),
                req.system_prompt,
                req.user_prompt_template.clone().unwrap_or_else(|| "{text}".to_string()),
                temp,
                enabled,
                target_lang
            ],
        ).map_err(|e| e.to_string())?;

        let masked_key = if raw_key.len() > 8 {
            format!("{}...{}", &raw_key[..4], &raw_key[raw_key.len()-4..])
        } else if !raw_key.is_empty() {
            "********".to_string()
        } else {
            "".to_string()
        };

        Ok(AIConfigModel {
            id,
            name: req.name.clone(),
            provider: req.provider.clone(),
            model: req.model.clone(),
            api_key_masked: masked_key,
            base_url: req.base_url.clone().unwrap_or_default(),
            system_prompt: req.system_prompt.clone(),
            user_prompt_template: req.user_prompt_template.clone().unwrap_or_else(|| "{text}".to_string()),
            temperature: temp,
            is_enabled: req.is_enabled.unwrap_or(true),
            target_language: target_lang,
        })
    }

    pub fn delete_ai_config(&self, id: &str) -> Result<bool, String> {
        let conn = self.conn.lock();
        let count = conn.execute("DELETE FROM ai_configs WHERE id = ?1", params![id])
            .map_err(|e| e.to_string())?;
        Ok(count > 0)
    }

    // Filter Rules
    pub fn get_all_filter_rules(&self) -> Result<Vec<FilterRuleModel>, String> {
        let conn = self.conn.lock();
        let mut stmt = conn.prepare(
            "SELECT id, name, whitelist_keywords, blacklist_keywords, regex_patterns, \
             allowed_media_types, blocked_media_types, drop_service_messages, min_message_length, max_message_length \
             FROM filter_rules ORDER BY name ASC"
        ).map_err(|e| e.to_string())?;

        let rows = stmt.query_map([], |row| {
            let wl_raw: String = row.get(2).unwrap_or_else(|_| "[]".to_string());
            let bl_raw: String = row.get(3).unwrap_or_else(|_| "[]".to_string());
            let rx_raw: String = row.get(4).unwrap_or_else(|_| "[]".to_string());
            let am_raw: String = row.get(5).unwrap_or_else(|_| "[]".to_string());
            let bm_raw: String = row.get(6).unwrap_or_else(|_| "[]".to_string());

            Ok(FilterRuleModel {
                id: row.get(0)?,
                name: row.get(1)?,
                whitelist_keywords: serde_json::from_str(&wl_raw).unwrap_or_default(),
                blacklist_keywords: serde_json::from_str(&bl_raw).unwrap_or_default(),
                regex_patterns: serde_json::from_str(&rx_raw).unwrap_or_default(),
                allowed_media_types: serde_json::from_str(&am_raw).unwrap_or_default(),
                blocked_media_types: serde_json::from_str(&bm_raw).unwrap_or_default(),
                drop_service_messages: row.get::<_, i32>(7).unwrap_or(1) != 0,
                min_message_length: row.get(8).unwrap_or(0),
                max_message_length: row.get(9).unwrap_or(0),
            })
        }).map_err(|e| e.to_string())?;

        let mut list = Vec::new();
        for r in rows {
            list.push(r.map_err(|e| e.to_string())?);
        }
        Ok(list)
    }

    pub fn save_filter_rule(&self, req: &SaveFilterRuleRequest) -> Result<FilterRuleModel, String> {
        let conn = self.conn.lock();
        let id = req.id.clone().unwrap_or_else(|| uuid::Uuid::new_v4().to_string());

        let wl_str = serde_json::to_string(&req.whitelist_keywords.clone().unwrap_or_default()).unwrap_or_else(|_| "[]".to_string());
        let bl_str = serde_json::to_string(&req.blacklist_keywords.clone().unwrap_or_default()).unwrap_or_else(|_| "[]".to_string());
        let rx_str = serde_json::to_string(&req.regex_patterns.clone().unwrap_or_default()).unwrap_or_else(|_| "[]".to_string());
        let am_str = serde_json::to_string(&req.allowed_media_types.clone().unwrap_or_default()).unwrap_or_else(|_| "[]".to_string());
        let bm_str = serde_json::to_string(&req.blocked_media_types.clone().unwrap_or_default()).unwrap_or_else(|_| "[]".to_string());
        let drop_srv = if req.drop_service_messages.unwrap_or(true) { 1 } else { 0 };
        let min_len = req.min_message_length.unwrap_or(0);
        let max_len = req.max_message_length.unwrap_or(0);

        conn.execute(
            "INSERT INTO filter_rules (
                id, name, whitelist_keywords, blacklist_keywords, regex_patterns,
                allowed_media_types, blocked_media_types, drop_service_messages, min_message_length, max_message_length
            ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                whitelist_keywords = excluded.whitelist_keywords,
                blacklist_keywords = excluded.blacklist_keywords,
                regex_patterns = excluded.regex_patterns,
                allowed_media_types = excluded.allowed_media_types,
                blocked_media_types = excluded.blocked_media_types,
                drop_service_messages = excluded.drop_service_messages,
                min_message_length = excluded.min_message_length,
                max_message_length = excluded.max_message_length",
            params![id, req.name, wl_str, bl_str, rx_str, am_str, bm_str, drop_srv, min_len, max_len],
        ).map_err(|e| e.to_string())?;

        Ok(FilterRuleModel {
            id,
            name: req.name.clone(),
            whitelist_keywords: req.whitelist_keywords.clone().unwrap_or_default(),
            blacklist_keywords: req.blacklist_keywords.clone().unwrap_or_default(),
            regex_patterns: req.regex_patterns.clone().unwrap_or_default(),
            allowed_media_types: req.allowed_media_types.clone().unwrap_or_default(),
            blocked_media_types: req.blocked_media_types.clone().unwrap_or_default(),
            drop_service_messages: req.drop_service_messages.unwrap_or(true),
            min_message_length: min_len,
            max_message_length: max_len,
        })
    }

    pub fn delete_filter_rule(&self, id: &str) -> Result<bool, String> {
        let conn = self.conn.lock();
        let count = conn.execute("DELETE FROM filter_rules WHERE id = ?1", params![id])
            .map_err(|e| e.to_string())?;
        Ok(count > 0)
    }

    // Pipeline Queue & DLQ
    pub fn get_delivery_jobs(&self, limit: usize) -> Result<Vec<DeliveryJobModel>, String> {
        let conn = self.conn.lock();
        let mut stmt = conn.prepare(
            "SELECT id, rule_id, source_chat_id, source_message_id, target_chat_id, status, \
             attempts, max_attempts, error_detail, delivery_stage, created_at \
             FROM delivery_jobs ORDER BY created_at DESC LIMIT ?1"
        ).map_err(|e| e.to_string())?;

        let rows = stmt.query_map(params![limit as i64], |row| {
            Ok(DeliveryJobModel {
                id: row.get(0)?,
                rule_id: row.get(1)?,
                source_chat_id: row.get(2)?,
                source_message_id: row.get(3)?,
                target_chat_id: row.get(4)?,
                status: row.get(5)?,
                attempts: row.get(6)?,
                max_attempts: row.get(7)?,
                error_detail: row.get(8).ok(),
                delivery_stage: row.get(9).unwrap_or_else(|_| "DIRECT".to_string()),
                created_at: row.get(10)?,
            })
        }).map_err(|e| e.to_string())?;

        let mut list = Vec::new();
        for r in rows {
            list.push(r.map_err(|e| e.to_string())?);
        }
        Ok(list)
    }

    pub fn get_dead_letter_queue(&self, limit: usize) -> Result<Vec<DeadLetterJobModel>, String> {
        let conn = self.conn.lock();
        let mut stmt = conn.prepare(
            "SELECT id, job_id, rule_id, source_chat_id, source_message_id, target_chat_id, \
             attempts, last_error, error_category, dead_lettered_at \
             FROM dead_letter_queue ORDER BY dead_lettered_at DESC LIMIT ?1"
        ).map_err(|e| e.to_string())?;

        let rows = stmt.query_map(params![limit as i64], |row| {
            Ok(DeadLetterJobModel {
                id: row.get(0)?,
                job_id: row.get(1).ok(),
                rule_id: row.get(2)?,
                source_chat_id: row.get(3)?,
                source_message_id: row.get(4)?,
                target_chat_id: row.get(5)?,
                attempts: row.get(6)?,
                last_error: row.get(7).unwrap_or_default(),
                error_category: row.get(8).unwrap_or_default(),
                dead_lettered_at: row.get(9)?,
            })
        }).map_err(|e| e.to_string())?;

        let mut list = Vec::new();
        for r in rows {
            list.push(r.map_err(|e| e.to_string())?);
        }
        Ok(list)
    }

    pub fn retry_dead_letter_job(&self, id: &str) -> Result<bool, String> {
        let conn = self.conn.lock();
        let dlq_row: Result<(String, String, i64, String, String), _> = conn.query_row(
            "SELECT rule_id, source_chat_id, source_message_id, target_chat_id, payload_json FROM dead_letter_queue WHERE id = ?1",
            params![id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?))
        );

        if let Ok((rule_id, src_id, src_msg_id, tgt_id, payload)) = dlq_row {
            let now = chrono::Utc::now().timestamp();
            let new_job_id = uuid::Uuid::new_v4().to_string();
            conn.execute(
                "INSERT OR REPLACE INTO delivery_jobs (id, rule_id, source_chat_id, source_message_id, target_chat_id, payload_json, status, attempts, created_at, updated_at) \
                 VALUES (?1, ?2, ?3, ?4, ?5, 'PENDING', 0, ?6, ?6)",
                params![new_job_id, rule_id, src_id, src_msg_id, tgt_id, payload, now]
            ).map_err(|e| e.to_string())?;

            conn.execute("DELETE FROM dead_letter_queue WHERE id = ?1", params![id]).map_err(|e| e.to_string())?;
            Ok(true)
        } else {
            Err("DLQ Job not found".to_string())
        }
    }

    pub fn purge_dead_letter_queue(&self) -> Result<usize, String> {
        let conn = self.conn.lock();
        let count = conn.execute("DELETE FROM dead_letter_queue", []).map_err(|e| e.to_string())?;
        Ok(count)
    }

    pub fn get_gateway_info(&self) -> Result<GatewaySystemInfo, String> {
        let conn = self.conn.lock();
        let total_rules: i64 = conn.query_row("SELECT count(*) FROM forward_rules", [], |r| r.get(0)).unwrap_or(0);
        let total_sessions: i64 = conn.query_row("SELECT count(*) FROM sessions", [], |r| r.get(0)).unwrap_or(0);

        let atf_core_online = std::net::TcpStream::connect("127.0.0.1:6001").is_ok();
        let atf_logger_online = std::net::TcpStream::connect("127.0.0.1:6002").is_ok();
        let atf_web_online = true;

        let db_size_bytes = std::fs::metadata(&self.db_path).map(|m| m.len()).unwrap_or(0);

        Ok(GatewaySystemInfo {
            atf_core_online,
            atf_logger_online,
            atf_web_online,
            uptime_seconds: 0,
            grpc_port: 6001,
            logger_port: 6002,
            web_port: 8088,
            version: "1.2.0".to_string(),
            db_size_bytes,
            total_rules,
            total_sessions,
        })
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
