# AiNxt Enterprise — documentation index
This directory holds **per-module reference documentation** — 507 pages, one
per significant module, router or component. It is reference material: it describes
what each part of the codebase does, and it assumes you already have the platform
running.

**If you are new, start elsewhere:**

| Start here | For |
|---|---|
| [`GETTING_STARTED.md`](GETTING_STARTED.md) | Prerequisites, install, configuration, first login, optional features, troubleshooting |
| [`../README.md`](../README.md) | What AiNxt is, one-command setup, architecture, LLM configuration |
| [`../SUPPORT.md`](../SUPPORT.md) | Where to look for answers |
| [`../compliance/README.md`](../compliance/README.md) | Third-party component inventories and licensing notes |

## Layout

Pages are filed in topic directories rather than in one flat folder. Grouping is
derived from file names, so a page may reasonably belong to more than one group —
when in doubt use the full listing further down, or your editor's file search.

| Directory | Covers | Pages |
|---|---|---|
| [`api/`](api/) | HTTP API & routers | 22 |
| [`agents/`](agents/) | Agents & orchestration | 38 |
| [`skills/`](skills/) | Skills, tools & integrations | 38 |
| [`documents/`](documents/) | Document processing | 52 |
| [`llm/`](llm/) | Models, routing & spend | 29 |
| [`sdlc/`](sdlc/) | SDLC & governance | 29 |
| [`ui/`](ui/) | UI components | 26 |
| [`chat/`](chat/) | Chat & collaboration | 24 |
| [`workers/`](workers/) | Background workers & scheduling | 24 |
| [`connectors/`](connectors/) | Connectors & integrations | 21 |
| [`knowledge/`](knowledge/) | Knowledge base, RAG & search | 20 |
| [`cowork/`](cowork/) | Buddy, Code, CLI & desktop | 18 |
| [`workflows/`](workflows/) | Workflows & triggers | 14 |
| [`observability/`](observability/) | Monitoring, metrics & evals | 13 |
| [`core/`](core/) | Infrastructure & config | 13 |
| [`auth/`](auth/) | Security, auth & compliance | 12 |
| [`security/`](security/) | Security policies & CIL | 12 |
| [`mcp/`](mcp/) | MCP servers & bridge | 11 |
| [`analytics/`](analytics/) | Spend, budgets & reporting | 10 |
| [`coach/`](coach/) | Evaluation & quality | 8 |
| [`sandbox/`](sandbox/) | Secure code execution | 7 |
| [`presentation/`](presentation/) | Presentation generation | 6 |
| [`products/`](products/) | Products & endpoints | 6 |

Pages are filed in the most relevant topic directory. If a page is not where you
expected, use your editor's file search across `docs/` — filenames are descriptive.

Two files stay at this level because they are entry points rather than reference
pages: this index and [`GETTING_STARTED.md`](GETTING_STARTED.md).

The full per-page listing follows, grouped the same way.

## UI & frontend components  (54)
<details>
<summary>Show pages</summary>

- [Canvas](ui/Canvas.md)
- [ChatActions](ui/ChatActions.md)
- [ChatPanel](ui/ChatPanel.md)
- [ChatPanelCore](ui/ChatPanelCore.md)
- [ConfigPanel](ui/ConfigPanel.md)
- [DebugLogView](ui/DebugLogView.md)
- [FileHandling](ui/FileHandling.md)
- [LLMProviderConfig](ui/LLMProviderConfig.md)
- [LoopItemsPicker](ui/LoopItemsPicker.md)
- [MessageContent](ui/MessageContent.md)
- [RunSettingsStrip](ui/RunSettingsStrip.md)
- [Sidebar](ui/Sidebar.md)
- [SubflowPicker](ui/SubflowPicker.md)
- [abstudio_frontend](ui/abstudio_frontend.md)
- [ai_ui_frontend_app_core](ui/ai_ui_frontend_app_core.md)
- [ai_ui_frontend_build_studio](ui/ai_ui_frontend_build_studio.md)
- [ai_ui_frontend_hooks](ui/ai_ui_frontend_hooks.md)
- [ai_ui_frontend_hooks_desktop](ui/ai_ui_frontend_hooks_desktop.md)
- [ai_ui_frontend_utils](ui/ai_ui_frontend_utils.md)
- [ai_ui_frontend_utils_chat_message](ui/ai_ui_frontend_utils_chat_message.md)
- [ai_ui_frontend_utils_file_preview](ui/ai_ui_frontend_utils_file_preview.md)
- [ai_ui_frontend_utils_ppt](ui/ai_ui_frontend_utils_ppt.md)
- [artifact_views](ui/artifact_views.md)
- [artifacts_panel](ui/artifacts_panel.md)
- [brand_mark](ui/brand_mark.md)
- [build_studio](ui/build_studio.md)
- [code_block](ui/code_block.md)
- [code_editor](ui/code_editor.md)
- [code_editor_diff](ui/code_editor_diff.md)
- [code_editor_explorer](ui/code_editor_explorer.md)
- [code_editor_panel](ui/code_editor_panel.md)
- [common_components](ui/common_components.md)
- [enhancement_features](ui/enhancement_features.md)
- [export_template](ui/export_template.md)
- [history_panel](ui/history_panel.md)
- [hooks](ui/hooks.md)
- [layout_helpers](ui/layout_helpers.md)
- [manifest](ui/manifest.md)
- [navigator_activity](ui/navigator_activity.md)
- [scope_picker](ui/scope_picker.md)
- [spinner](ui/spinner.md)
- [trigger_modal](ui/trigger_modal.md)
- [triggers_feature](ui/triggers_feature.md)
- [ui_dialog](ui/ui_dialog.md)
- [utils](ui/utils.md)
- [utils_editor_persistence](ui/utils_editor_persistence.md)
- [utils_make_id](ui/utils_make_id.md)
- [utils_thread_helpers](ui/utils_thread_helpers.md)
- [voice_and_tts](ui/voice_and_tts.md)
- [voice_mic](ui/voice_mic.md)
- [voice_mode](ui/voice_mode.md)
- [whisper_service](ui/whisper_service.md)
- [work_item_panel](ui/work_item_panel.md)
- [workspace_utilities](ui/workspace_utilities.md)

</details>

## LLM gateway, routing, budget & governance  (37)
<details>
<summary>Show pages</summary>

- [advanced_reasoning](llm/advanced_reasoning.md)
- [budget](llm/budget.md)
- [budget_manager](llm/budget_manager.md)
- [budget_router](llm/budget_router.md)
- [claude_gateway](llm/claude_gateway.md)
- [compression_service](llm/compression_service.md)
- [context_engine](llm/context_engine.md)
- [core_llm_handler](llm/core_llm_handler.md)
- [gemini_gateway](llm/gemini_gateway.md)
- [level_overrides](llm/level_overrides.md)
- [llm_proxy](llm/llm_proxy.md)
- [llm_proxy_core_circuit_breaker](llm/llm_proxy_core_circuit_breaker.md)
- [llm_proxy_core_claude_cache](llm/llm_proxy_core_claude_cache.md)
- [llm_proxy_core_logger](llm/llm_proxy_core_logger.md)
- [llm_proxy_core_retry](llm/llm_proxy_core_retry.md)
- [llm_proxy_gateway_claude](llm/llm_proxy_gateway_claude.md)
- [llm_proxy_gateway_gemini](llm/llm_proxy_gateway_gemini.md)
- [llm_proxy_gateway_openai](llm/llm_proxy_gateway_openai.md)
- [llm_proxy_main](llm/llm_proxy_main.md)
- [local_llm_gateway](llm/local_llm_gateway.md)
- [model_and_tool_listing](llm/model_and_tool_listing.md)
- [model_governance](llm/model_governance.md)
- [model_governance_router](llm/model_governance_router.md)
- [model_routing](llm/model_routing.md)
- [model_routing_core](llm/model_routing_core.md)
- [ollama_gateway](llm/ollama_gateway.md)
- [openai_compatible_endpoints](llm/openai_compatible_endpoints.md)
- [openai_gateway](llm/openai_gateway.md)
- [profiles](llm/profiles.md)
- [profiles_resolution](llm/profiles_resolution.md)
- [profiles_routing](llm/profiles_routing.md)
- [profiles_schema](llm/profiles_schema.md)
- [profiles_shaping](llm/profiles_shaping.md)
- [reaction_engines](llm/reaction_engines.md)
- [reaction_engines_react_loop](llm/reaction_engines_react_loop.md)
- [reaction_engines_recovery](llm/reaction_engines_recovery.md)
- [router_policy](llm/router_policy.md)

</details>

## SDLC & governance  (35)
<details>
<summary>Show pages</summary>

- [approval_actions](sdlc/approval_actions.md)
- [core_governance](sdlc/core_governance.md)
- [core_governance_client](sdlc/core_governance_client.md)
- [diff_approval](sdlc/diff_approval.md)
- [governance](sdlc/governance.md)
- [governance_actions](sdlc/governance_actions.md)
- [governance_feature](sdlc/governance_feature.md)
- [governance_router](sdlc/governance_router.md)
- [multi_repo_approval](sdlc/multi_repo_approval.md)
- [review_router](sdlc/review_router.md)
- [run_diff_tools](sdlc/run_diff_tools.md)
- [sdlc_agent_loop](sdlc/sdlc_agent_loop.md)
- [sdlc_baseline_gate](sdlc/sdlc_baseline_gate.md)
- [sdlc_cli_engine](sdlc/sdlc_cli_engine.md)
- [sdlc_coder_tools](sdlc/sdlc_coder_tools.md)
- [sdlc_gate_signal](sdlc/sdlc_gate_signal.md)
- [sdlc_governance](sdlc/sdlc_governance.md)
- [sdlc_governance_config](sdlc/sdlc_governance_config.md)
- [sdlc_governance_config_2](sdlc/sdlc_governance_config_2.md)
- [sdlc_governance_review](sdlc/sdlc_governance_review.md)
- [sdlc_loop_tools](sdlc/sdlc_loop_tools.md)
- [sdlc_metrics](sdlc/sdlc_metrics.md)
- [sdlc_normalizer](sdlc/sdlc_normalizer.md)
- [sdlc_patch_engine](sdlc/sdlc_patch_engine.md)
- [sdlc_pipeline](sdlc/sdlc_pipeline.md)
- [sdlc_pipeline_agents](sdlc/sdlc_pipeline_agents.md)
- [sdlc_pipeline_core](sdlc/sdlc_pipeline_core.md)
- [sdlc_pipeline_stepper](sdlc/sdlc_pipeline_stepper.md)
- [sdlc_pipeline_workers](sdlc/sdlc_pipeline_workers.md)
- [sdlc_planning_artifact](sdlc/sdlc_planning_artifact.md)
- [sdlc_router](sdlc/sdlc_router.md)
- [sdlc_state_machine](sdlc/sdlc_state_machine.md)
- [sdlc_status_model](sdlc/sdlc_status_model.md)
- [secure_code_gate_router](sdlc/secure_code_gate_router.md)
- [shared_core_sdlc_pipeline](sdlc/shared_core_sdlc_pipeline.md)

</details>

## Skills & tools  (32)
<details>
<summary>Show pages</summary>

- [ats_tools](skills/ats_tools.md)
- [data_tools](skills/data_tools.md)
- [dslar_skills](skills/dslar_skills.md)
- [dslar_skills_dslar_clause_chunking](skills/dslar_skills_dslar_clause_chunking.md)
- [dslar_skills_dslar_image_enrichment](skills/dslar_skills_dslar_image_enrichment.md)
- [dslar_skills_dslar_pdf_extraction](skills/dslar_skills_dslar_pdf_extraction.md)
- [dslar_skills_dslar_report_rendering](skills/dslar_skills_dslar_report_rendering.md)
- [lms_tools](skills/lms_tools.md)
- [shared_core_tools](skills/shared_core_tools.md)
- [shared_integrations](skills/shared_integrations.md)
- [shared_integrations_connector_adapters](skills/shared_integrations_connector_adapters.md)
- [shared_integrations_connector_adapters_atlassian](skills/shared_integrations_connector_adapters_atlassian.md)
- [shared_integrations_connector_adapters_cloud_productivity](skills/shared_integrations_connector_adapters_cloud_productivity.md)
- [shared_integrations_connector_infrastructure](skills/shared_integrations_connector_infrastructure.md)
- [shared_integrations_connector_infrastructure_dpi_consent](skills/shared_integrations_connector_infrastructure_dpi_consent.md)
- [shared_integrations_connector_infrastructure_engine](skills/shared_integrations_connector_infrastructure_engine.md)
- [shared_integrations_connector_infrastructure_mcp_bridge](skills/shared_integrations_connector_infrastructure_mcp_bridge.md)
- [shared_integrations_connector_infrastructure_metrics](skills/shared_integrations_connector_infrastructure_metrics.md)
- [shared_integrations_connector_infrastructure_oauth2](skills/shared_integrations_connector_infrastructure_oauth2.md)
- [shared_integrations_connector_infrastructure_registry](skills/shared_integrations_connector_infrastructure_registry.md)
- [shared_skills](skills/shared_skills.md)
- [skill_factory_pipeline](skills/skill_factory_pipeline.md)
- [skill_proposals](skills/skill_proposals.md)
- [skills_feature](skills/skills_feature.md)
- [skills_router](skills/skills_router.md)
- [specialized_skills](skills/specialized_skills.md)
- [specialized_skills_dpdp_onboarding](skills/specialized_skills_dpdp_onboarding.md)
- [tool_integration](skills/tool_integration.md)
- [tool_utilities](skills/tool_utilities.md)
- [tools](skills/tools.md)
- [tools_canonical_seed](skills/tools_canonical_seed.md)
- [tools_feature](skills/tools_feature.md)

</details>

## Core infrastructure & shared services  (30)
<details>
<summary>Show pages</summary>

- [ainxt_scripts](core/ainxt_scripts.md)
- [app_core](core/app_core.md)
- [app_main](core/app_main.md)
- [app_models](core/app_models.md)
- [config](core/config.md)
- [constants](core/constants.md)
- [core_config](core/core_config.md)
- [core_infrastructure](core/core_infrastructure.md)
- [core_infrastructure_config_logging](core/core_infrastructure_config_logging.md)
- [core_infrastructure_observability](core/core_infrastructure_observability.md)
- [core_infrastructure_resilience_storage](core/core_infrastructure_resilience_storage.md)
- [dep_table](core/dep_table.md)
- [dependency_utilities](core/dependency_utilities.md)
- [dependency_utilities_manifest_writer](core/dependency_utilities_manifest_writer.md)
- [dependency_utilities_resolution](core/dependency_utilities_resolution.md)
- [gateway](core/gateway.md)
- [gunicorn_config](core/gunicorn_config.md)
- [middleware](core/middleware.md)
- [middleware_client_source](core/middleware_client_source.md)
- [middleware_request_id](core/middleware_request_id.md)
- [open_questions](core/open_questions.md)
- [overview](core/overview.md)
- [pipeline](core/pipeline.md)
- [pipeline_core](core/pipeline_core.md)
- [scripts](core/scripts.md)
- [scripts_utilities](core/scripts_utilities.md)
- [shared_api_routers](core/shared_api_routers.md)
- [shared_core](core/shared_core.md)
- [shared_features](core/shared_features.md)
- [skeleton](core/skeleton.md)

</details>

## Connectors & integrations  (28)
<details>
<summary>Show pages</summary>

- [broadcast_coach_workers](connectors/broadcast_coach_workers.md)
- [broadcast_coach_workers_broadcast](connectors/broadcast_coach_workers_broadcast.md)
- [broadcast_coach_workers_coach](connectors/broadcast_coach_workers_coach.md)
- [broadcast_coach_workers_graph_edges](connectors/broadcast_coach_workers_graph_edges.md)
- [broadcast_router](connectors/broadcast_router.md)
- [calendar_tools](connectors/calendar_tools.md)
- [confluence_tools](connectors/confluence_tools.md)
- [connector_adapters](connectors/connector_adapters.md)
- [connector_adapters_enterprise_collab](connectors/connector_adapters_enterprise_collab.md)
- [connector_infrastructure](connectors/connector_infrastructure.md)
- [connectors](connectors/connectors.md)
- [connectors_integrations](connectors/connectors_integrations.md)
- [connectors_router](connectors/connectors_router.md)
- [email_broadcast](connectors/email_broadcast.md)
- [email_tools](connectors/email_tools.md)
- [github_tools](connectors/github_tools.md)
- [gitlab_tools](connectors/gitlab_tools.md)
- [graph_webhooks_router](connectors/graph_webhooks_router.md)
- [jira_tools](connectors/jira_tools.md)
- [n8n_router](connectors/n8n_router.md)
- [n8n_tools](connectors/n8n_tools.md)
- [slack_router](connectors/slack_router.md)
- [teams_config](connectors/teams_config.md)
- [teams_integration](connectors/teams_integration.md)
- [teams_router](connectors/teams_router.md)
- [tools_m365_bridge](connectors/tools_m365_bridge.md)
- [webhooks_router](connectors/webhooks_router.md)
- [zoho_router](connectors/zoho_router.md)

</details>

## Cowork desktop agent  (27)
<details>
<summary>Show pages</summary>

- [browser_automation_extension](cowork/browser_automation_extension.md)
- [browser_automation_extension_background](cowork/browser_automation_extension_background.md)
- [browser_automation_extension_content](cowork/browser_automation_extension_content.md)
- [browser_automation_extension_llm](cowork/browser_automation_extension_llm.md)
- [cli_runtime](cowork/cli_runtime.md)
- [cli_runtime_runner](cowork/cli_runtime_runner.md)
- [cli_runtime_session](cowork/cli_runtime_session.md)
- [cli_updates_router](cowork/cli_updates_router.md)
- [cowork_admin_router](cowork/cowork_admin_router.md)
- [cowork_canvas](cowork/cowork_canvas.md)
- [cowork_conversations_router](cowork/cowork_conversations_router.md)
- [cowork_desktop](cowork/cowork_desktop.md)
- [cowork_dispatch_router](cowork/cowork_dispatch_router.md)
- [cowork_enterprise](cowork/cowork_enterprise.md)
- [cowork_mcp_router](cowork/cowork_mcp_router.md)
- [cowork_policy_router](cowork/cowork_policy_router.md)
- [cowork_projects_router](cowork/cowork_projects_router.md)
- [cowork_settings](cowork/cowork_settings.md)
- [cowork_tasks_router](cowork/cowork_tasks_router.md)
- [cowork_usage_router](cowork/cowork_usage_router.md)
- [desktop_app](cowork/desktop_app.md)
- [desktop_app_browser_automation](cowork/desktop_app_browser_automation.md)
- [desktop_app_computer_use](cowork/desktop_app_computer_use.md)
- [desktop_app_cowork_engine](cowork/desktop_app_cowork_engine.md)
- [desktop_app_main_process](cowork/desktop_app_main_process.md)
- [desktop_router](cowork/desktop_router.md)
- [dev_workspace](cowork/dev_workspace.md)

</details>

## Agents  (24)
<details>
<summary>Show pages</summary>

- [agent_analytics](agents/agent_analytics.md)
- [agent_factory_pipeline](agents/agent_factory_pipeline.md)
- [agent_management](agents/agent_management.md)
- [agent_orchestration](agents/agent_orchestration.md)
- [agent_system](agents/agent_system.md)
- [agents_catalog](agents/agents_catalog.md)
- [agents_feature](agents/agents_feature.md)
- [agents_feature_card](agents/agents_feature_card.md)
- [agents_feature_dashboard](agents/agents_feature_dashboard.md)
- [agents_feature_editor](agents/agents_feature_editor.md)
- [agents_feature_factory_chat](agents/agents_feature_factory_chat.md)
- [agents_router](agents/agents_router.md)
- [checkpoint](agents/checkpoint.md)
- [checkpoint_agent_chat_store](agents/checkpoint_agent_chat_store.md)
- [core_agent_framework](agents/core_agent_framework.md)
- [core_factory_utils](agents/core_factory_utils.md)
- [engine_loop_evaluator](agents/engine_loop_evaluator.md)
- [engine_native_engine](agents/engine_native_engine.md)
- [loop_models](agents/loop_models.md)
- [loop_runner](agents/loop_runner.md)
- [swarm](agents/swarm.md)
- [swarm_execution](agents/swarm_execution.md)
- [swarm_planning](agents/swarm_planning.md)
- [tools_swarm_spawn](agents/tools_swarm_spawn.md)

</details>

## Chat & messaging  (23)
<details>
<summary>Show pages</summary>

- [cached_ask_router](chat/cached_ask_router.md)
- [chat](chat/chat.md)
- [chat_and_messaging](chat/chat_and_messaging.md)
- [chat_router](chat/chat_router.md)
- [chat_settings](chat/chat_settings.md)
- [core_chat](chat/core_chat.md)
- [core_chat_logic](chat/core_chat_logic.md)
- [discussions](chat/discussions.md)
- [discussions_router](chat/discussions_router.md)
- [discussions_service](chat/discussions_service.md)
- [feedback](chat/feedback.md)
- [feedback_and_sharing](chat/feedback_and_sharing.md)
- [feedback_router](chat/feedback_router.md)
- [inbox](chat/inbox.md)
- [inbox_router](chat/inbox_router.md)
- [mailbox_router](chat/mailbox_router.md)
- [message](chat/message.md)
- [message_actions](chat/message_actions.md)
- [message_meta](chat/message_meta.md)
- [messages_compat_router](chat/messages_compat_router.md)
- [notifications_router](chat/notifications_router.md)
- [threads](chat/threads.md)
- [threads_router](chat/threads_router.md)

</details>

## HTTP API & routers  (22)
<details>
<summary>Show pages</summary>

- [admin_router](api/admin_router.md)
- [api_agent_chat](api/api_agent_chat.md)
- [api_agent_templates](api/api_agent_templates.md)
- [api_agents](api/api_agents.md)
- [api_catalog](api/api_catalog.md)
- [api_chat](api/api_chat.md)
- [api_deps](api/api_deps.md)
- [api_documents](api/api_documents.md)
- [api_execution](api/api_execution.md)
- [api_factories](api/api_factories.md)
- [api_generation](api/api_generation.md)
- [api_governance](api/api_governance.md)
- [api_kb](api/api_kb.md)
- [api_loops](api/api_loops.md)
- [api_template_admin](api/api_template_admin.md)
- [api_templates](api/api_templates.md)
- [api_triggers](api/api_triggers.md)
- [api_workflows](api/api_workflows.md)
- [prompt_mgmt_router](api/prompt_mgmt_router.md)
- [task_tracker_tools](api/task_tracker_tools.md)
- [templates_feature](api/templates_feature.md)
- [templates_router](api/templates_router.md)

</details>

## Knowledge base, RAG & search  (22)
<details>
<summary>Show pages</summary>

- [core_infrastructure_resilience_storage](core/core_infrastructure_resilience_storage.md)
- [embedding_service](knowledge/embedding_service.md)
- [embedding_service_cache](knowledge/embedding_service_cache.md)
- [index_router](knowledge/index_router.md)
- [indexers](knowledge/indexers.md)
- [indexing_and_search](knowledge/indexing_and_search.md)
- [kb_chat](knowledge/kb_chat.md)
- [kb_chat_chat_settings](knowledge/kb_chat_chat_settings.md)
- [kb_chat_core_chat](knowledge/kb_chat_core_chat.md)
- [kb_chat_enhancement_features](knowledge/kb_chat_enhancement_features.md)
- [kb_chat_export_template](knowledge/kb_chat_export_template.md)
- [kb_chat_file_image_handling](knowledge/kb_chat_file_image_handling.md)
- [kb_chat_list](knowledge/kb_chat_list.md)
- [kb_chat_panel](knowledge/kb_chat_panel.md)
- [kb_graph](knowledge/kb_graph.md)
- [kb_router](knowledge/kb_router.md)
- [kb_search_tools](knowledge/kb_search_tools.md)
- [knowledge_base](knowledge/knowledge_base.md)
- [knowledge_graph](knowledge/knowledge_graph.md)
- [knowledge_graph_router](knowledge/knowledge_graph_router.md)
- [shared_core_knowledge_base](knowledge/shared_core_knowledge_base.md)
- [shared_core_knowledge_base_document_store](knowledge/shared_core_knowledge_base_document_store.md)
- [shared_core_knowledge_base_entity_registry](knowledge/shared_core_knowledge_base_entity_registry.md)

</details>

## Background workers & scheduling  (22)
<details>
<summary>Show pages</summary>

- [ChatActions](ui/ChatActions.md)
- [ChatPanel](ui/ChatPanel.md)
- [ChatPanelCore](ui/ChatPanelCore.md)
- [MessageContent](ui/MessageContent.md)
- [ai_ui_frontend_utils_chat_message](ui/ai_ui_frontend_utils_chat_message.md)
- [chat](chat/chat.md)
- [chat_and_messaging](chat/chat_and_messaging.md)
- [chat_settings](chat/chat_settings.md)
- [core_chat](chat/core_chat.md)
- [core_chat_logic](chat/core_chat_logic.md)
- [discussions](chat/discussions.md)
- [discussions_service](chat/discussions_service.md)
- [documents_chat](documents/documents_chat.md)
- [email_broadcast](connectors/email_broadcast.md)
- [inbox](chat/inbox.md)
- [message](chat/message.md)
- [message_actions](chat/message_actions.md)
- [message_meta](chat/message_meta.md)
- [ppt_chat](presentation/ppt_chat.md)
- [threads](chat/threads.md)
- [utils_thread_helpers](ui/utils_thread_helpers.md)
- [workflows_feature_editor_chat_panel](workflows/workflows_feature_editor_chat_panel.md)
- [workflows_feature_factory_chat](workflows/workflows_feature_factory_chat.md)

</details>

## Models, routing & spend  (29)
<details>
<summary>Show pages</summary>

- [app_models](core/app_models.md)
- [browser_automation_extension_llm](cowork/browser_automation_extension_llm.md)
- [budget](llm/budget.md)
- [budget_manager](llm/budget_manager.md)
- [budget_team_panel](analytics/budget_team_panel.md)
- [budget_utilization_view](analytics/budget_utilization_view.md)
- [claude_gateway](llm/claude_gateway.md)
- [core_llm_handler](llm/core_llm_handler.md)
- [gateway](core/gateway.md)
- [gemini_gateway](llm/gemini_gateway.md)
- [llm_proxy](llm/llm_proxy.md)
- [llm_proxy_core_circuit_breaker](llm/llm_proxy_core_circuit_breaker.md)
- [llm_proxy_core_claude_cache](llm/llm_proxy_core_claude_cache.md)
- [llm_proxy_core_logger](llm/llm_proxy_core_logger.md)
- [llm_proxy_core_retry](llm/llm_proxy_core_retry.md)
- [llm_proxy_gateway_claude](llm/llm_proxy_gateway_claude.md)
- [llm_proxy_gateway_gemini](llm/llm_proxy_gateway_gemini.md)
- [llm_proxy_gateway_openai](llm/llm_proxy_gateway_openai.md)
- [llm_proxy_main](llm/llm_proxy_main.md)
- [llm_spend](analytics/llm_spend.md)
- [llm_spend_fetchers](analytics/llm_spend_fetchers.md)
- [local_llm_gateway](llm/local_llm_gateway.md)
- [loop_models](agents/loop_models.md)
- [model_and_tool_listing](llm/model_and_tool_listing.md)
- [model_routing](llm/model_routing.md)
- [model_routing_core](llm/model_routing_core.md)
- [ollama_gateway](llm/ollama_gateway.md)
- [openai_gateway](llm/openai_gateway.md)
- [services_budget_digest](workers/services_budget_digest.md)

</details>

## SDLC & governance  (29)
<details>
<summary>Show pages</summary>

- [approval_actions](sdlc/approval_actions.md)
- [core_governance](sdlc/core_governance.md)
- [core_governance_client](sdlc/core_governance_client.md)
- [diff_approval](sdlc/diff_approval.md)
- [governance](sdlc/governance.md)
- [governance_actions](sdlc/governance_actions.md)
- [governance_feature](sdlc/governance_feature.md)
- [model_governance](llm/model_governance.md)
- [multi_repo_approval](sdlc/multi_repo_approval.md)
- [sdlc_baseline_gate](sdlc/sdlc_baseline_gate.md)
- [sdlc_cli_engine](sdlc/sdlc_cli_engine.md)
- [sdlc_coder_tools](sdlc/sdlc_coder_tools.md)
- [sdlc_gate_signal](sdlc/sdlc_gate_signal.md)
- [sdlc_governance](sdlc/sdlc_governance.md)
- [sdlc_governance_config](sdlc/sdlc_governance_config.md)
- [sdlc_governance_config_2](sdlc/sdlc_governance_config_2.md)
- [sdlc_governance_review](sdlc/sdlc_governance_review.md)
- [sdlc_loop_tools](sdlc/sdlc_loop_tools.md)
- [sdlc_metrics](sdlc/sdlc_metrics.md)
- [sdlc_normalizer](sdlc/sdlc_normalizer.md)
- [sdlc_patch_engine](sdlc/sdlc_patch_engine.md)
- [sdlc_pipeline](sdlc/sdlc_pipeline.md)
- [sdlc_pipeline_core](sdlc/sdlc_pipeline_core.md)
- [sdlc_pipeline_stepper](sdlc/sdlc_pipeline_stepper.md)
- [sdlc_planning_artifact](sdlc/sdlc_planning_artifact.md)
- [sdlc_state_machine](sdlc/sdlc_state_machine.md)
- [sdlc_status_model](sdlc/sdlc_status_model.md)
- [security_and_governance](security/security_and_governance.md)
- [shared_core_sdlc_pipeline](sdlc/shared_core_sdlc_pipeline.md)

</details>

## Connectors & integrations  (21)
<details>
<summary>Show pages</summary>

- [confluence_tools](connectors/confluence_tools.md)
- [connector_adapters](connectors/connector_adapters.md)
- [connector_adapters_enterprise_collab](connectors/connector_adapters_enterprise_collab.md)
- [connector_infrastructure](connectors/connector_infrastructure.md)
- [connectors](connectors/connectors.md)
- [connectors_integrations](connectors/connectors_integrations.md)
- [github_tools](connectors/github_tools.md)
- [gitlab_tools](connectors/gitlab_tools.md)
- [jira_tools](connectors/jira_tools.md)
- [shared_integrations_connector_adapters](skills/shared_integrations_connector_adapters.md)
- [shared_integrations_connector_adapters_atlassian](skills/shared_integrations_connector_adapters_atlassian.md)
- [shared_integrations_connector_adapters_cloud_productivity](skills/shared_integrations_connector_adapters_cloud_productivity.md)
- [shared_integrations_connector_infrastructure](skills/shared_integrations_connector_infrastructure.md)
- [shared_integrations_connector_infrastructure_dpi_consent](skills/shared_integrations_connector_infrastructure_dpi_consent.md)
- [shared_integrations_connector_infrastructure_engine](skills/shared_integrations_connector_infrastructure_engine.md)
- [shared_integrations_connector_infrastructure_mcp_bridge](skills/shared_integrations_connector_infrastructure_mcp_bridge.md)
- [shared_integrations_connector_infrastructure_metrics](skills/shared_integrations_connector_infrastructure_metrics.md)
- [shared_integrations_connector_infrastructure_oauth2](skills/shared_integrations_connector_infrastructure_oauth2.md)
- [shared_integrations_connector_infrastructure_registry](skills/shared_integrations_connector_infrastructure_registry.md)
- [teams_config](connectors/teams_config.md)
- [teams_integration](connectors/teams_integration.md)

</details>

## Security, auth & compliance  (12)
<details>
<summary>Show pages</summary>

- [auth](auth/auth.md)
- [authentication](auth/authentication.md)
- [authentication_dependencies](auth/authentication_dependencies.md)
- [authentication_ldap](auth/authentication_ldap.md)
- [authentication_rbac](auth/authentication_rbac.md)
- [authentication_sso](auth/authentication_sso.md)
- [decision_engines_compliance](security/decision_engines_compliance.md)
- [guardrails](security/guardrails.md)
- [guardrails_tools](security/guardrails_tools.md)
- [privacy_service](security/privacy_service.md)
- [security_privacy](security/security_privacy.md)
- [security_scan_tools](security/security_scan_tools.md)

</details>

## Background workers & scheduling  (24)
<details>
<summary>Show pages</summary>

- [broadcast_coach_workers](connectors/broadcast_coach_workers.md)
- [broadcast_coach_workers_broadcast](connectors/broadcast_coach_workers_broadcast.md)
- [broadcast_coach_workers_coach](connectors/broadcast_coach_workers_coach.md)
- [broadcast_coach_workers_graph_edges](connectors/broadcast_coach_workers_graph_edges.md)
- [chat_agent_execution_workers](workers/chat_agent_execution_workers.md)
- [chat_agent_execution_workers_chat_agent](workers/chat_agent_execution_workers_chat_agent.md)
- [chat_agent_execution_workers_workflow](workers/chat_agent_execution_workers_workflow.md)
- [cowork_scheduler](workers/cowork_scheduler.md)
- [cowork_scheduling_workers](workers/cowork_scheduling_workers.md)
- [cowork_scheduling_workers_scheduler](workers/cowork_scheduling_workers_scheduler.md)
- [cowork_scheduling_workers_task_worker](workers/cowork_scheduling_workers_task_worker.md)
- [document_knowledge_workers](workers/document_knowledge_workers.md)
- [external_integration_workers](workers/external_integration_workers.md)
- [external_integration_workers_codebase_indexing](workers/external_integration_workers_codebase_indexing.md)
- [infrastructure_maintenance_workers](workers/infrastructure_maintenance_workers.md)
- [infrastructure_maintenance_workers_dlq](workers/infrastructure_maintenance_workers_dlq.md)
- [infrastructure_maintenance_workers_memory](workers/infrastructure_maintenance_workers_memory.md)
- [infrastructure_maintenance_workers_purge](workers/infrastructure_maintenance_workers_purge.md)
- [infrastructure_maintenance_workers_scheduling](workers/infrastructure_maintenance_workers_scheduling.md)
- [kafka_event_consumer](workers/kafka_event_consumer.md)
- [services](workers/services.md)
- [services_budget_digest](workers/services_budget_digest.md)
- [services_services](workers/services_services.md)
- [services_trigger_scheduler](workers/services_trigger_scheduler.md)
- [worker_orchestration](workers/worker_orchestration.md)
- [workers](workers/workers.md)

</details>

## Document processing  (20)
<details>
<summary>Show pages</summary>

- [core_ocr](documents/core_ocr.md)
- [doc_download_router](documents/doc_download_router.md)
- [doc_generation](documents/doc_generation.md)
- [doc_generator](documents/doc_generator.md)
- [docs_router](documents/docs_router.md)
- [document_preview](documents/document_preview.md)
- [document_processing](documents/document_processing.md)
- [document_processing_docling_parser](documents/document_processing_docling_parser.md)
- [document_processing_paddle_ocr](documents/document_processing_paddle_ocr.md)
- [document_tools](documents/document_tools.md)
- [documents](documents/documents.md)
- [documents_chat](documents/documents_chat.md)
- [documents_generation](documents/documents_generation.md)
- [documents_guide](documents/documents_guide.md)
- [documents_preview](documents/documents_preview.md)
- [file_and_asset_serving](documents/file_and_asset_serving.md)
- [file_image_handling](documents/file_image_handling.md)
- [local_files](documents/local_files.md)
- [office](documents/office.md)
- [office_addin](documents/office_addin.md)

</details>

## Workflows & triggers  (19)
<details>
<summary>Show pages</summary>

- [core_workflow_repo](workflows/core_workflow_repo.md)
- [workflow_editor](workflows/workflow_editor.md)
- [workflow_editor_conditions](workflows/workflow_editor_conditions.md)
- [workflow_editor_conditions_cases](workflows/workflow_editor_conditions_cases.md)
- [workflow_editor_conditions_loop](workflows/workflow_editor_conditions_loop.md)
- [workflow_editor_edges](workflows/workflow_editor_edges.md)
- [workflow_editor_nodes](workflows/workflow_editor_nodes.md)
- [workflow_editor_nodes_branching](workflows/workflow_editor_nodes_branching.md)
- [workflow_editor_nodes_control_flow](workflows/workflow_editor_nodes_control_flow.md)
- [workflow_editor_nodes_execution](workflows/workflow_editor_nodes_execution.md)
- [workflow_factory_pipeline](workflows/workflow_factory_pipeline.md)
- [workflow_management](workflows/workflow_management.md)
- [workflow_preview](workflows/workflow_preview.md)
- [workflow_system](workflows/workflow_system.md)
- [workflows_feature](workflows/workflows_feature.md)
- [workflows_feature_dashboard](workflows/workflows_feature_dashboard.md)
- [workflows_feature_editor_canvas](workflows/workflows_feature_editor_canvas.md)
- [workflows_feature_editor_chat_panel](workflows/workflows_feature_editor_chat_panel.md)
- [workflows_feature_factory_chat](workflows/workflows_feature_factory_chat.md)

</details>

## Security & compliance  (18)
<details>
<summary>Show pages</summary>

- [audit_and_tracing](security/audit_and_tracing.md)
- [audit_router](security/audit_router.md)
- [cil](security/cil.md)
- [cil_intent](security/cil_intent.md)
- [cil_lexical](security/cil_lexical.md)
- [cil_policy](security/cil_policy.md)
- [compliance_router](security/compliance_router.md)
- [compliance_scan_router](security/compliance_scan_router.md)
- [decision_engines](security/decision_engines.md)
- [decision_engines_compliance](security/decision_engines_compliance.md)
- [decision_engines_core](security/decision_engines_core.md)
- [decision_engines_hardblock](security/decision_engines_hardblock.md)
- [guardrails](security/guardrails.md)
- [guardrails_tools](security/guardrails_tools.md)
- [privacy_service](security/privacy_service.md)
- [security_and_governance](security/security_and_governance.md)
- [security_privacy](security/security_privacy.md)
- [security_scan_tools](security/security_scan_tools.md)

</details>

## Auth, identity & access  (16)
<details>
<summary>Show pages</summary>

- [auth](auth/auth.md)
- [auth_router](auth/auth_router.md)
- [authentication](auth/authentication.md)
- [authentication_dependencies](auth/authentication_dependencies.md)
- [authentication_ldap](auth/authentication_ldap.md)
- [authentication_rbac](auth/authentication_rbac.md)
- [authentication_sso](auth/authentication_sso.md)
- [ckms](auth/ckms.md)
- [dpi_consent](auth/dpi_consent.md)
- [login](auth/login.md)
- [profile](auth/profile.md)
- [profile_router](auth/profile_router.md)
- [scim_router](auth/scim_router.md)
- [session_router](auth/session_router.md)
- [user_management](auth/user_management.md)
- [vault_router](auth/vault_router.md)

</details>

## MCP servers & bridge  (15)
<details>
<summary>Show pages</summary>

- [api_mcp](mcp/api_mcp.md)
- [core_mcp_manager](mcp/core_mcp_manager.md)
- [mcp_governance_router](mcp/mcp_governance_router.md)
- [mcp_server_router](mcp/mcp_server_router.md)
- [mcp_servers](mcp/mcp_servers.md)
- [mcp_servers_base](mcp/mcp_servers_base.md)
- [mcp_servers_collaboration](mcp/mcp_servers_collaboration.md)
- [mcp_servers_content](mcp/mcp_servers_content.md)
- [mcp_servers_data](mcp/mcp_servers_data.md)
- [mcp_servers_platform](mcp/mcp_servers_platform.md)
- [mcp_servers_productivity](mcp/mcp_servers_productivity.md)
- [mcp_system](mcp/mcp_system.md)
- [mcp_system_registry](mcp/mcp_system_registry.md)
- [mcp_system_registry_master](mcp/mcp_system_registry_master.md)
- [mcp_system_registry_tools](mcp/mcp_system_registry_tools.md)

</details>

## Observability, monitoring & evals  (13)
<details>
<summary>Show pages</summary>

- [dept_metrics](observability/dept_metrics.md)
- [dept_metrics_router](observability/dept_metrics_router.md)
- [evals_dashboard](observability/evals_dashboard.md)
- [evals_evolution](observability/evals_evolution.md)
- [evals_evolution_evaluation](observability/evals_evolution_evaluation.md)
- [evals_evolution_tier2](observability/evals_evolution_tier2.md)
- [evals_router](observability/evals_router.md)
- [health_and_monitoring](observability/health_and_monitoring.md)
- [monitoring](observability/monitoring.md)
- [observability](observability/observability.md)
- [observability_metrics](observability/observability_metrics.md)
- [observability_tracing](observability/observability_tracing.md)
- [trace_panel](observability/trace_panel.md)

</details>

## Presentations (PPT)  (11)
<details>
<summary>Show pages</summary>

- [ppt_chat](presentation/ppt_chat.md)
- [ppt_detection](presentation/ppt_detection.md)
- [ppt_wizard](presentation/ppt_wizard.md)
- [presenton_lib](presentation/presenton_lib.md)
- [presenton_lib_api_client](presentation/presenton_lib_api_client.md)
- [presenton_lib_layout_mapping](presentation/presenton_lib_layout_mapping.md)
- [presenton_lib_layout_registry](presentation/presenton_lib_layout_registry.md)
- [presenton_lib_payload_builder](presentation/presenton_lib_payload_builder.md)
- [presenton_lib_stream_reader](presentation/presenton_lib_stream_reader.md)
- [presenton_patches](presentation/presenton_patches.md)
- [presenton_router](presentation/presenton_router.md)

</details>

## Products, endpoints & projects  (9)
<details>
<summary>Show pages</summary>

- [api_keys_router](products/api_keys_router.md)
- [endpoint_manager](products/endpoint_manager.md)
- [endpoint_mgmt_router](products/endpoint_mgmt_router.md)
- [endpoint_proxy_router](products/endpoint_proxy_router.md)
- [marketplace_router](products/marketplace_router.md)
- [product_manager](products/product_manager.md)
- [products_router](products/products_router.md)
- [projects](products/projects.md)
- [projects_router](products/projects_router.md)

</details>

## Usage analytics & spend reporting  (8)
<details>
<summary>Show pages</summary>

- [budget_team_panel](analytics/budget_team_panel.md)
- [budget_utilization_view](analytics/budget_utilization_view.md)
- [digest_hod_router](analytics/digest_hod_router.md)
- [digest_manager_router](analytics/digest_manager_router.md)
- [llm_spend](analytics/llm_spend.md)
- [llm_spend_fetchers](analytics/llm_spend_fetchers.md)
- [llm_spend_report_router](analytics/llm_spend_report_router.md)
- [monthly_statement_router](analytics/monthly_statement_router.md)

</details>

## Sandboxed execution  (6)
<details>
<summary>Show pages</summary>

- [docker_execution_tool](sandbox/docker_execution_tool.md)
- [sandbox](sandbox/sandbox.md)
- [sandbox_docker_execution](sandbox/sandbox_docker_execution.md)
- [sandbox_document_execution](sandbox/sandbox_document_execution.md)
- [sandbox_image_building](sandbox/sandbox_image_building.md)
- [sandbox_router](sandbox/sandbox_router.md)

</details>

## Coach & evaluation  (5)
<details>
<summary>Show pages</summary>

- [coach](coach/coach.md)
- [coach_admin](coach/coach_admin.md)
- [coach_admin_router](coach/coach_admin_router.md)
- [coach_router](coach/coach_router.md)
- [coach_system](coach/coach_system.md)

</details>

## Codebase indexing & IDE  (5)
<details>
<summary>Show pages</summary>

- [code](codebase/code.md)
- [code_conversations_router](codebase/code_conversations_router.md)
- [codebase_manager](codebase/codebase_manager.md)
- [ide_router](codebase/ide_router.md)
- [jobs_router](codebase/jobs_router.md)

</details>

## Generated artifacts

Not documentation pages — output from the tooling that produced this directory:

- `first_module_tree.json`
- `index.html`
- `metadata.json`
- `module_tree.json`
