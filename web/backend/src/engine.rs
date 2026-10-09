use crate::models::*;
use regex::Regex;

pub struct SmartSimulator;

impl SmartSimulator {
    pub fn simulate(rule: &ForwardRuleModel, req: &SimulateRequest) -> SimulateResponse {
        let text = req.test_text.trim();
        let criteria = rule.detection_criteria.as_object();
        let match_mode = criteria
            .and_then(|c| c.get("match_mode"))
            .and_then(|v| v.as_str())
            .unwrap_or("ANY")
            .to_uppercase();

        let mut conditions_summary = Vec::new();
        let mut matches = Vec::new();
        let mut conditions_checked = 0;

        // 1. Origin check
        let has_forward_metadata = req.forward_origin_chat_id.is_some()
            || req.forward_origin_title.is_some()
            || req.forward_origin_username.is_some();

        let origin_ids: Vec<String> = criteria
            .and_then(|c| c.get("forward_origin_chat_ids").or_else(|| c.get("origin_chat_ids")))
            .and_then(|v| v.as_array())
            .map(|arr| arr.iter().filter_map(|x| x.as_str().map(|s| s.trim().to_string())).collect())
            .unwrap_or_default();

        let origin_titles: Vec<String> = criteria
            .and_then(|c| c.get("forward_origin_titles").or_else(|| c.get("origin_titles")))
            .and_then(|v| v.as_array())
            .map(|arr| arr.iter().filter_map(|x| x.as_str().map(|s| s.trim().to_lowercase())).collect())
            .unwrap_or_default();

        let has_origin_filter = !origin_ids.is_empty() || !origin_titles.is_empty();

        if has_origin_filter {
            conditions_checked += 1;
            conditions_summary.push(format!("فیلتر منشأ: شناسه‌ها [{}] و نام‌ها [{}]", origin_ids.join(", "), origin_titles.join(", ")));

            if has_forward_metadata {
                let f_id = req.forward_origin_chat_id.as_deref().unwrap_or("").trim();
                let f_title = req.forward_origin_title.as_deref().unwrap_or("").trim().to_lowercase();
                let f_user = req.forward_origin_username.as_deref().unwrap_or("").trim().trim_start_matches('@').to_lowercase();

                let id_match = origin_ids.iter().any(|target| {
                    target == f_id || target.trim_start_matches('@').to_lowercase() == f_user
                });
                let title_match = origin_titles.iter().any(|target| f_title.contains(target));

                if id_match || title_match {
                    matches.push("origin_matched");
                }
            }
        }

        // 2. Keyword checks
        let keywords: Vec<String> = criteria
            .and_then(|c| c.get("text_contains").or_else(|| c.get("keywords")))
            .and_then(|v| v.as_array())
            .map(|arr| arr.iter().filter_map(|x| x.as_str().map(|s| s.trim().to_lowercase())).collect())
            .unwrap_or_default();

        if !keywords.is_empty() {
            conditions_checked += 1;
            conditions_summary.push(format!("فیلتر کلیدواژه‌ها: [{}]", keywords.join(", ")));
            let text_lower = text.to_lowercase();
            if keywords.iter().any(|kw| text_lower.contains(kw)) {
                matches.push("keyword_matched");
            }
        }

        // 3. Regex check
        let regex_p = criteria
            .and_then(|c| c.get("regex_pattern").or_else(|| c.get("text_regex")))
            .and_then(|v| v.as_str())
            .unwrap_or("");

        if !regex_p.is_empty() {
            conditions_checked += 1;
            conditions_summary.push(format!("فیلتر Regex: `{}`", regex_p));
            if let Ok(re) = Regex::new(regex_p) {
                if re.is_match(text) {
                    matches.push("regex_matched");
                }
            }
        }

        // VIP decision
        let (is_vip, vip_reason) = if conditions_checked == 0 {
            // No explicit filters defined: if has forward metadata and category is VIP, treat as VIP
            if has_forward_metadata {
                (true, "تطبیق خودکار بر اساس متادیتای فوروارد تلگرام".to_string())
            } else {
                (false, "پیام بدون متادیتای فوروارد (عادی / UNKNOWN_ORIGIN)".to_string())
            }
        } else if match_mode == "ALL" {
            if matches.len() == conditions_checked {
                (true, format!("تحقق کامل تمام شروط الزامی ({})", matches.join(" + ")))
            } else {
                (false, format!("عدم تحقق تمام شروط الزامی (تنها {} از {} شرط برقرار شد)", matches.len(), conditions_checked))
            }
        } else {
            // ANY
            if !matches.is_empty() {
                (true, format!("تحقق شرط ({}) بر اساس منطق ANY", matches.join(" یا ")))
            } else {
                (false, "هیچ‌کدام از شروط تطبیق داده نشد".to_string())
            }
        };

        let detected_category = if is_vip {
            "VIP".to_string()
        } else if !has_forward_metadata {
            "NORMAL".to_string()
        } else {
            "UNKNOWN_ORIGIN".to_string()
        };

        // Determine Route Decision
        let cat_config = rule.message_category.to_uppercase();
        let mut matched = rule.is_active && !rule.is_paused;
        let mut reason = String::new();

        if !matched {
            reason = "قانون در وضعیت غیرفعال یا متوقف (Paused) قرار دارد.".to_string();
        } else if (cat_config == "VIP" || cat_config == "VIP_ONLY") && !is_vip {
            matched = false;
            reason = format!("عدم تطبیق با رده VIP: {}", vip_reason);
        } else if (cat_config == "NORMAL" || cat_config == "NORMAL_ONLY") && is_vip {
            matched = false;
            reason = "پیام رده VIP است در حالی که قانون تنها برای پیام‌های عادی تعریف شده است.".to_string();
        }

        let mut decision = "DROP".to_string();
        let mut route_label_fa = "⛔ عدم ارسال (عدم تطبیق)".to_string();
        let mut route_label_en = "DROP (No Match)".to_string();
        let mut action_steps = Vec::new();

        let is_protected = req.is_protected.unwrap_or(false);
        let mut protected_handled = false;

        if matched {
            if rule.use_intermediate && !rule.intermediate_channel_id.is_empty() && is_vip {
                decision = "BRANDING_VIA_C".to_string();
                route_label_fa = "💎 مسیر ۲: برندینگ واسط (A ➔ C ➔ B)".to_string();
                route_label_en = "Route 2: VIP Branding Hop (A -> C -> B)".to_string();

                if is_protected {
                    protected_handled = true;
                    action_steps.push(format!(
                        "1️⃣ کانال A قفل است: دانلود کلاینت و Re-upload تمیز به کانال C ({})",
                        rule.intermediate_channel_name.as_str()
                    ));
                } else {
                    action_steps.push(format!(
                        "1️⃣ کپی سروری بدون تگ (Clean Copy) به کانال C ({})",
                        rule.intermediate_channel_name.as_str()
                    ));
                }
                action_steps.push(format!(
                    "2️⃣ فوروارد رسمی (Native Forward) از C به B ({}) با هدر برند واسط C",
                    rule.target_chat_name.as_str()
                ));
                action_steps.push("3️⃣ محافظت ضد لوپ: پیام ثبت‌شده در C هرگز دوباره توسط ربات پردازش نخواهد شد.".to_string());
            } else if rule.forward_mode == "DIRECT_FORWARD" {
                decision = "DIRECT_FORWARD".to_string();
                route_label_fa = "↗️ مسیر ۳: فوروارد نیتیو مستقیم (A ➔ B)".to_string();
                route_label_en = "Route 3: Native Forward (A -> B)".to_string();

                if is_protected {
                    protected_handled = true;
                    action_steps.push("⚠️ کانال A قفل است! تنزل خودکار به Clean Copy / Re-upload طبق سیاست Fallback.".to_string());
                    action_steps.push(format!("• تحویل تمیز به مقصد B ({})", rule.target_chat_name.as_str()));
                } else {
                    action_steps.push(format!("• فوروارد رسمی تلگرام مستقیماً به مقصد B ({})", rule.target_chat_name.as_str()));
                }
            } else {
                decision = "DIRECT_COPY".to_string();
                route_label_fa = "📋 مسیر ۱: ارسال مستقیم و تمیز (A ➔ B)".to_string();
                route_label_en = "Route 1: Direct Clean Copy (A -> B)".to_string();

                if is_protected {
                    protected_handled = true;
                    action_steps.push("🛡 بای‌پس حفاظت: دانلود موقت مدیا و Re-upload با حفظ انتیتی‌ها.".to_string());
                }
                action_steps.push(format!("• ارسال مستقیم کپی به مقصد B ({}) بدون ورود به کانال واسط C", rule.target_chat_name.as_str()));
            }
        } else {
            action_steps.push("• هیچ پیامی ارسال نخواهد شد.".to_string());
        }

        // Preview message formulation
        let mut preview_parts = Vec::new();
        if !rule.custom_header.is_empty() {
            preview_parts.push(rule.custom_header.clone());
            preview_parts.push("────────".to_string());
        }
        preview_parts.push(text.to_string());
        if !rule.custom_footer.is_empty() {
            preview_parts.push("────────".to_string());
            preview_parts.push(rule.custom_footer.clone());
        }
        let preview_message = preview_parts.join("\n");

        SimulateResponse {
            matched,
            is_vip,
            detected_category,
            decision,
            route_label_fa,
            route_label_en,
            reason: if reason.is_empty() { vip_reason } else { reason },
            conditions_summary,
            action_steps,
            preview_message,
            protected_content_handled: protected_handled,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_rule() -> ForwardRuleModel {
        ForwardRuleModel {
            id: "rule_1".to_string(),
            session_id: "sess_1".to_string(),
            source_chat_id: "-1001111111111".to_string(),
            source_chat_name: "Source A".to_string(),
            target_chat_id: "-1003333333333".to_string(),
            target_chat_name: "Target B".to_string(),
            routing_type: "CUSTOM".to_string(),
            forward_mode: "CUSTOM_HEADER_COPY".to_string(),
            is_active: true,
            is_paused: false,
            priority: 10,
            message_category: "VIP_ONLY".to_string(),
            use_intermediate: true,
            intermediate_channel_id: "-1002222222222".to_string(),
            intermediate_channel_name: "Brand C".to_string(),
            fallback_mode: "COPY_MESSAGE".to_string(),
            fallback_enabled: true,
            detection_criteria: serde_json::json!({
                "match_mode": "ANY",
                "text_contains": ["VIP", "SIGNAL"],
                "forward_origin_chat_ids": ["-1001111111111"]
            }),
            link_policy: "PRESERVE_ALL".to_string(),
            custom_header: "💎 VIP FOREX".to_string(),
            custom_footer: "".to_string(),
            split_long_caption: true,
            created_at: 0,
            updated_at: 0,
        }
    }

    #[test]
    fn test_vip_message_triggers_branding_via_c() {
        let rule = sample_rule();
        let req = SimulateRequest {
            rule_id: None,
            test_text: "VIP BUY GOLD @ 2650".to_string(),
            forward_origin_chat_id: Some("-1001111111111".to_string()),
            forward_origin_title: Some("Origin Channel".to_string()),
            forward_origin_username: None,
            has_media: Some(false),
            media_type: None,
            is_protected: Some(false),
            rule_override: None,
        };

        let res = SmartSimulator::simulate(&rule, &req);
        assert!(res.matched);
        assert!(res.is_vip);
        assert_eq!(res.decision, "BRANDING_VIA_C");
        assert!(res.route_label_fa.contains("مسیر ۲"));
        assert!(res.action_steps.iter().any(|s| s.contains("Clean Copy")));
    }

    #[test]
    fn test_normal_message_does_not_hop_c_when_vip_only() {
        let rule = sample_rule();
        let req = SimulateRequest {
            rule_id: None,
            test_text: "یک پیام معمولی بدون کلیدواژه".to_string(),
            forward_origin_chat_id: None,
            forward_origin_title: None,
            forward_origin_username: None,
            has_media: Some(false),
            media_type: None,
            is_protected: Some(false),
            rule_override: None,
        };

        let res = SmartSimulator::simulate(&rule, &req);
        assert!(!res.matched);
        assert!(!res.is_vip);
        assert_eq!(res.decision, "DROP");
    }

    #[test]
    fn test_direct_route_copy() {
        let mut rule = sample_rule();
        rule.message_category = "ALL".to_string();
        rule.use_intermediate = false;
        rule.forward_mode = "COPY_MESSAGE".to_string();

        let req = SimulateRequest {
            rule_id: None,
            test_text: "یک پیام عمومی".to_string(),
            forward_origin_chat_id: None,
            forward_origin_title: None,
            forward_origin_username: None,
            has_media: Some(false),
            media_type: None,
            is_protected: Some(false),
            rule_override: None,
        };

        let res = SmartSimulator::simulate(&rule, &req);
        assert!(res.matched);
        assert_eq!(res.decision, "DIRECT_COPY");
        assert!(res.route_label_fa.contains("مسیر ۱"));
    }

    #[test]
    fn test_protected_content_handled() {
        let rule = sample_rule();
        let req = SimulateRequest {
            rule_id: None,
            test_text: "VIP BUY GOLD".to_string(),
            forward_origin_chat_id: Some("-1001111111111".to_string()),
            forward_origin_title: None,
            forward_origin_username: None,
            has_media: Some(true),
            media_type: Some("photo".to_string()),
            is_protected: Some(true),
            rule_override: None,
        };

        let res = SmartSimulator::simulate(&rule, &req);
        assert!(res.protected_content_handled);
        assert!(res.action_steps.iter().any(|s| s.contains("Re-upload")));
    }
}

