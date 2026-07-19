# -*- coding: utf-8 -*-
"""
Report Generation and Delivery
Mode-specific templates for Pre-Market, Afternoon, and Post-Market strategies.
"""
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from datetime import datetime
import pandas as pd
from core.config import Config
import re
from core.utils import get_level_description
from core.display_labels import (
    display_account,
    display_action,
    display_risk_level,
    display_status,
    humanize_text,
)

logger = logging.getLogger("StockAnalyzer.Reporter")


def _is_email_content_log_enabled():
    try:
        return bool(getattr(Config, "LOG_EMAIL_CONTENT", True))
    except Exception:
        return True


def _log_email_snapshot(subject, content, *, status, extra=None):
    if not _is_email_content_log_enabled():
        logger.info("EMAIL_LOG status=%s subject=%s content_logging=disabled", status, subject)
        return
    safe_content = str(content or "").replace("\r\n", "\n").replace("\r", "\n")
    safe_subject = str(subject or "")
    boundary = "=" * 24
    logger.info(
        "\n%s EMAIL %s %s\nSubject: %s\nLength: %s chars%s\n%s\n%s END EMAIL %s %s",
        boundary,
        status,
        boundary,
        safe_subject,
        len(safe_content),
        f"\nExtra: {extra}" if extra else "",
        safe_content,
        boundary,
        status,
        boundary,
    )


def _log_email_result(subject, *, status, extra=None):
    logger.info("EMAIL_RESULT status=%s subject=%s%s", status, subject, f" extra={extra}" if extra else "")


def _is_sim_account(account):
    return str(account or "").lower().startswith("paper_")


def log_report_snapshot(title, content, *, source="report"):
    """Write generated report content to logs even when email is disabled."""
    if not _is_email_content_log_enabled():
        logger.info("REPORT_LOG source=%s title=%s content_logging=disabled", source, title)
        return
    safe_content = str(content or "").replace("\r\n", "\n").replace("\r", "\n")
    safe_title = str(title or source or "report")
    boundary = "=" * 24
    logger.info(
        "\n%s REPORT %s %s\nTitle: %s\nLength: %s chars\n%s\n%s END REPORT %s %s",
        boundary,
        source,
        boundary,
        safe_title,
        len(safe_content),
        safe_content,
        boundary,
        source,
        boundary,
    )

def _text_to_html(text, images_to_embed=None):
    """Convert custom plain text report and simple Markdown to HTML"""
    lines = text.split('\n')
    html = [
        '<!DOCTYPE html>',
        '<html>',
        '<head><meta charset="utf-8"></head>',
        '<body style="background-color: #f6f8fa; padding: 20px;">',
        '<div style="font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; background-color: #ffffff; padding: 20px; border-radius: 8px; line-height: 1.6; color: #333333; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">'
    ]
    
    in_table = False
    headers = []
    in_list = False
    
    import re
    
    for line in lines:
        stripped = line.strip()
        
        # Table end detection
        if in_table and not stripped.startswith('|') and not stripped.startswith('===') and not stripped.startswith('---'):
            html.append('</tbody></table>')
            in_table = False
            
        # List end detection
        if in_list and not (stripped.startswith('- ') or stripped.startswith('* ') or re.match(r'^\d+\.\s', stripped)):
            html.append('</ul>')
            in_list = False
            
        if not stripped:
            continue
            
        # Handle separators
        if stripped.startswith('===') or (stripped.startswith('---') and len(stripped) >= 3):
            if in_table and stripped.startswith('---'):
                pass  # Skip inner separators
            elif not in_table and stripped.startswith('---') and len(html) > 0 and '代码' in html[-1]:
                # Found a table header
                pass
            else:
                html.append('<hr style="border: 1px solid #eee; margin: 15px 0;">')
            continue
            
        # Headings
        if stripped.startswith('#'):
            level = len(stripped) - len(stripped.lstrip('#'))
            title_text = stripped.lstrip('#').strip()
            title_text = re.sub(r'\*\*(.*?)\*\*', r'\1', title_text)
            sizes = {1: '24px', 2: '20px', 3: '18px', 4: '16px'}
            size = sizes.get(level, '14px')
            # Custom T+0 Heading Highlight
            if '日内网格做T指导策略' in title_text:
                border = "border-bottom: 2px solid #e74c3c; padding-bottom: 5px; margin-bottom: 15px; display: inline-block; color: #e74c3c; background-color: #fff3cd; padding: 5px 10px; border-radius: 4px;"
            else:
                border = "border-bottom: 2px solid #3498db; padding-bottom: 5px; margin-bottom: 15px; display: inline-block;" if level <= 3 else "margin-bottom: 5px; margin-top: 20px; font-weight: bold;"
            html.append(f'<h{level} style="color: #2c3e50; font-size: {size}; {border}">{title_text}</h{level}>')
            continue
            
        # Legacy Section titles
        if stripped.startswith('【') or stripped.startswith('🔭【') or stripped.startswith('📦【') or stripped.startswith('📈【') or stripped.startswith('🔴【') or stripped.startswith('🟢【'):
            html.append(f'<h3 style="color: #2c3e50; margin-top: 25px; border-bottom: 2px solid #3498db; padding-bottom: 5px; margin-bottom: 15px; display: inline-block;">{stripped}</h3>')
            continue
            
        # Table detection
        if stripped.startswith('|') and stripped.endswith('|'):
            if not in_table:
                in_table = True
                raw_headers = stripped.strip('|').split('|')
                headers = [h.strip() for h in raw_headers if h.strip()]
                html.append('<table style="width: 100%; border-collapse: collapse; margin: 15px 0; font-size: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); white-space: nowrap;">')
                html.append('<thead><tr style="background-color: #f8f9fa; border-bottom: 2px solid #dee2e6;">')
                for h in headers:
                    html.append(f'<th style="padding: 12px 8px; text-align: center; color: #495057; font-weight: bold;">{h}</th>')
                html.append('</tr></thead><tbody>')
                continue
                
            if '---' in line:
                continue
                
            inner_content = stripped.strip('|')
            cells = [cell.strip() for cell in inner_content.split('|')]
            
            html.append('<tr style="border-bottom: 1px solid #e9ecef;">')
            for p in cells[:len(headers)]:
                color = "inherit"
                p_clean = re.sub(r'<[^>]+>', '', p)
                if "+" in p_clean or "涨停" in p_clean or "吃肉" in p_clean or p_clean.startswith('买'): color = "#e74c3c" # Red
                elif "-" in p_clean or "吃面" in p_clean or p_clean.startswith('卖'): color = "#2ecc71" # Green
                
                p_html = re.sub(r'\*\*(.*?)\*\*', r'<strong style="color: #1a1a1a;">\1</strong>', p)
                html.append(f'<td style="padding: 10px 8px; text-align: center; color: {color};">{p_html}</td>')
            html.append('</tr>')
            continue
            
        # Lists
        if stripped.startswith('- ') or stripped.startswith('* ') or re.match(r'^\d+\.\s', stripped):
            if not in_list:
                html.append('<ul style="margin: 10px 0; padding-left: 20px; color: #495057;">')
                in_list = True
            
            if stripped.startswith('- ') or stripped.startswith('* '):
                li_content = stripped[2:]
            else:
                li_content = re.sub(r'^\d+\.\s', '', stripped)
                
            li_html = re.sub(r'\*\*(.*?)\*\*', r'<strong style="color: #1a1a1a;">\1</strong>', li_content)
            
            color = "#495057"
            if "🔴" in li_content: color = "#2ecc71"
            elif "🟢" in li_content or "🔥" in li_content or "⭐" in li_content or "📈" in li_content: color = "#e74c3c"
            
            # Highlight T+0 Advice Items
            if "日内多空枢轴" in li_content or "超买做T抛压" in li_content or "超卖做T低吸" in li_content or "最新均价" in li_content:
                html.append(f'<li style="margin-bottom: 8px; color: #d35400; font-weight: bold;">{li_html}</li>')
            else:
                html.append(f'<li style="margin-bottom: 8px; color: {color};">{li_html}</li>')
            continue
            
        # Normal paragraphs
        if stripped.startswith('>'):
            block_content = re.sub(r'\*\*(.*?)\*\*', r'<strong style="color: #1a1a1a;">\1</strong>', stripped[1:].strip())
            html.append(f'<div style="background-color: #f8f9fa; border-left: 4px solid #ced4da; padding: 10px; margin: 10px 0; color: #495057; font-size: 13px;">{block_content}</div>')
        elif stripped.startswith('*注:'):
            p_html = re.sub(r'\*\*(.*?)\*\*', r'<strong style="color: #555;">\1</strong>', stripped)
            html.append(f'<p style="color: #95a5a6; font-size: 12px; margin: 15px 0 5px 0; border-top: 1px dashed #eee; padding-top: 10px;">{p_html}</p>')
        elif stripped.startswith('![') and '](file:///' in stripped:
            m = re.search(r'!\[([^\]]*)\]\(file:///(.*?)\)', stripped)
            if m:
                alt = m.group(1)
                path = m.group(2)
                import uuid
                cid = f"img_{uuid.uuid4().hex[:8]}"
                if images_to_embed is not None:
                    images_to_embed[cid] = path
                html.append(f'<div style="text-align: center; margin: 20px 0; background: #fafafa; padding: 10px; border: 1px solid #eee; border-radius: 4px;"><img src="cid:{cid}" alt="{alt}" style="max-width: 100%; height: auto; display: inline-block;"><br><span style="color: #999; font-size: 12px;">{alt}</span></div>')
            continue
        else:
            color = "#333333"
            if "🔴" in stripped: color = "#2ecc71"
            elif "🟢" in stripped or "🔥" in stripped or "⭐" in stripped or "📈" in stripped: color = "#e74c3c"
            
            p_html = re.sub(r'\*\*(.*?)\*\*', r'<strong style="color: #1a1a1a;">\1</strong>', stripped)
            html.append(f'<p style="margin: 8px 0; color: {color};">{p_html}</p>')
            
    if in_table:
        html.append('</tbody></table>')
    if in_list:
        html.append('</ul>')
        
    html.append('</div>')
    html.append('</body>')
    html.append('</html>')
    return "\n".join(html)

class Reporter:
    def format_report(self, result, mode='pre_market', evolution_info=None):
        """
        Format analysis result into mode-specific text report
        
        Args:
            result: Analysis result dict
            mode: Report mode (pre_market/afternoon/post_market/watchlist)
            evolution_info: Optional dict with evolution/briefing info
        """
        if 'error' in result:
            return f"分析失败: {humanize_text(result['error'])}"
        
        # [V10] Build evolution briefing if provided
        evolution_section = ""
        if evolution_info:
            lines = []
            lines.append("🧬【V10 进化简报】")
            
            # Weather info
            try:
                from core.utils import normalize_weather
                weather = normalize_weather(evolution_info.get('weather', '☀️晴天'))
            except Exception:
                weather = evolution_info.get('weather', '☀️晴天')
            lines.append(f"  🌤️ 市场天气: {weather}")
            
            # Negative filter stats
            rejected = evolution_info.get('rejected_count', 0)
            lines.append(f"  🚫 负面过滤器: 拦截 {rejected} 只")
            
            # Sector bonuses
            sector_bonuses = evolution_info.get('sector_bonuses', [])
            if sector_bonuses:
                lines.append(f"  🔥 板块共振加分: {', '.join(sector_bonuses)}")
            
            lines.append("")
            evolution_section = "\n".join(lines)
        
        if mode == 'afternoon':
            return self._format_afternoon(result, evolution_section)
        elif mode == 'post_market':
            return self._format_post_market(result)
        elif mode == 'watchlist':
            return self._format_watchlist(result)
        else:
            return self._format_pre_market(result, evolution_section)
    
    # =========================================
    # PRE-MARKET (09:25) - 竞价 + MACD 筛选
    # =========================================
    def _format_pre_market(self, result, evolution_section=""):
        lines = []
        now = datetime.now()
        date_str = now.strftime('%Y-%m-%d')
        hour = now.hour
        
        # [V10] Add evolution briefing at the top
        if evolution_section:
            lines.append(evolution_section)
        
        # Time-aware title
        title = "🌅 早盘竞价分析报告" if hour < 10 else "📋 午间体检 - 早盘战果汇总"
        
        lines.append("=" * 60)
        lines.append(f"{title} ({date_str})")
        lines.append("策略: 高开2-5% + 换手>0.5% + MACD金叉&零轴上")
        lines.append("=" * 60)
        lines.append("")
        
        # Portfolio
        self._append_portfolio(lines, result)

        # Market environment (VNext)
        env = result.get('market_env', {}) or {}
        if env:
            sentiment = env.get('sentiment', {}) or {}
            eco = env.get('ecosystem', {}) or {}
            lines.append("🌤️【市场环境】")
            try:
                from core.utils import normalize_weather
                w_disp = normalize_weather(env.get('weather', '-'))
            except Exception:
                w_disp = env.get('weather','-')
            lines.append(f"  天气: {w_disp} | 风险: {display_risk_level(env.get('risk_level','-'))} | {humanize_text(env.get('message',''))}")
            lines.append(f"  涨停/跌停: {sentiment.get('limit_up',0)}/{sentiment.get('limit_down',0)}")
            if eco:
                height = eco.get('limit_up_height')
                ladder = eco.get('ladder_distribution') or {}
                if height is not None:
                    lines.append(f"  最高板: {height}")
                if ladder:
                    ladder_str = " ".join([f"{k}板:{v}" for k, v in ladder.items()])
                    lines.append(f"  梯队: {ladder_str}")
            # Concept heat (best-effort)
            ch = result.get('concept_heat') or {}
            top = ch.get('top') or []
            if top:
                top_str = " | ".join([f"{t.get('concept')}({t.get('count')})" for t in top[:8] if t.get('concept')])
                if top_str:
                    lines.append(f"  题材热度: {top_str}")
            lines.append("")

        self._append_today_focus(lines, result)

        # === KEY SECTION 1: Auction Picks (This is the CORE of pre-market) ===
        auc = result.get('auction_picks', [])
        if auc:
            lines.append(f"🎯【竞价评分选股】Top 15")
            lines.append(f"| 代码 | 名称 | 评分 | 竞价涨幅 | 换手率 | 成交额(亿) | 行业 | 模型 | 标签 |")
            lines.append(f"|:---:|:---|---:|---:|---:|---:|:---|:---|:---|")
            for s in auc[:15]:
                tag = s.get('zt_tag', '') or ''
                first_board_tag = s.get('first_board_tag', '') or ''
                if first_board_tag:
                    tag = f"{tag}/{first_board_tag}" if tag else first_board_tag
                lhb_tag = ""
                if s.get('lhb_present_today'):
                    lhb_tag = "LHB"
                if lhb_tag:
                    tag = f"{tag}/{lhb_tag}" if tag else lhb_tag
                if not tag:
                    tag = self._format_candidate_tag(s, default='基础竞价')
                model_cell = self._format_combo_model_cell(s)
                lines.append(f"| {s['code']} | {s['name']} | {s.get('score',0):.1f} | {s['open_change']:.2f}% | {s['turnover']:.2f}% | {s.get('amount_yi',0):.2f} | {s['industry']} | {model_cell} | {tag} |")
            lines.append("")
        else:
            lines.append("【竞价高开精选】暂无符合条件个股")
            lines.append("")
        
        # === KEY SECTION 1.5: Cold Start Picks ===
        cold = result.get('cold_start_picks', [])
        if cold:
            lines.append(f"❄️【冷启动选股】昨日未涨+今日突然启动")
            lines.append(f"| 代码 | 名称 | 评分 | 竞价涨幅 | 换手率 | 昨日涨幅 | 5日涨幅 | 资金流 | 模型 | 标签 |")
            lines.append(f"|:---:|:---|---:|---:|---:|---:|---:|---:|:---|:---|")
            for s in cold[:10]:
                prev_str = f"{s.get('prev_change', 0):.1f}%"
                if s.get('prev_zt'):
                    prev_str = "涨停"
                mf_str = f"{s.get('mf_intensity', 0):.1f}"
                model_cell = self._format_combo_model_cell(s)
                tag = self._format_candidate_tag(s, default='冷启动观察')
                lines.append(f"| {s['code']} | {s['name']} | {s.get('score',0):.1f} | {s['open_change']:.2f}% | {s['turnover']:.2f}% | {prev_str} | {s.get('gain_5d', 0):.1f}% | {mf_str} | {model_cell} | {tag} |")
            lines.append("")
        else:
            lines.append("【冷启动选股】暂无符合条件个股")
            lines.append("")
        
        # === KEY SECTION 2: Limit Up Analysis ===
        zt = result.get('limit_up_analysis', {})
        if zt and zt.get('count', 0) > 0:
            lines.append(f"📊【涨停热点】共 {zt.get('count', 0)} 只涨停")
            lines.append(f"| 板块 | 涨停数 | 龙头 | 连板 | 热度 |")
            lines.append(f"|:---|---:|:---|---:|---:|")
            for sec in zt.get('sectors', [])[:5]:
                hb = sec.get('highest_board', {})
                hb_name = hb.get('name', '-')[:8] if hb else '-'
                limit_times = hb.get('limit_times', 1) if hb else 0
                lines.append(f"| {sec['sector']} | {sec['count']} | {hb_name} | {limit_times} | {sec['score']:.0f} |")
            lines.append("")
        
        # === SECTION 3: MACD Screened Results ===
        hot = result.get('hot_stocks', [])
        if hot:
            lines.append(f"📈【MACD金叉精选】共 {result.get('candidates_count', 0)} 只通过筛选")
            lines.append("条件: 换手2-8% | 涨幅2-5% | 股价>均价1% | MACD金叉&零轴上")
            lines.append(f"| 代码 | 名称 | 现价 | 涨幅 | 换手 | 成交(亿) | 评级 |")
            lines.append(f"|:---:|:---|---:|---:|---:|---:|:---|")
            for s in hot[:10]:
                level = get_level_description(s)
                lines.append(f"| {s['code']} | {s['name']} | {s['price']:.2f} | {s['change']:+.2f}% | {s['turnover']:.2f}% | {s['amount']:.2f} | {level} |")
            lines.append("")
        
        # Sector Summary
        secs = result.get('sector_analysis', [])
        if secs:
            lines.append("【板块热点 (Top 5)】")
            for sec in secs:
                top_s = ", ".join(sec.get('top_stocks', []))
                lines.append(f"  {sec['sector']}: {sec['count']}只, 均涨+{sec['avg_change']:.2f}%, {sec['amount']:.1f}亿")
                lines.append(f"    > {top_s}")
            lines.append("")
        
        # Recommendation
        self._append_recommendation(lines, result)
        self._append_execution_audit(lines, result)
        
        # === DB Summary ===
        self._append_db_summary(lines, result, 'pre_market')
        
        return "\n".join(lines)
    
    # =========================================
    # AFTERNOON (14:30) - 板块资金流 + 个股精选
    # =========================================
    def _format_afternoon(self, result, evolution_section=""):
        lines = []
        date_str = datetime.now().strftime('%Y-%m-%d')
        
        # [V10] Add evolution briefing at the top
        if evolution_section:
            lines.append(evolution_section)
        
        lines.append("=" * 60)
        lines.append(f"☀️ 午盘资金流分析报告 ({date_str})")
        lines.append("策略: 10日板块资金流排名 → 实时涨幅筛选 → 精选个股")
        lines.append("=" * 60)
        lines.append("")
        
        # Portfolio
        self._append_portfolio(lines, result)

        # Market environment (VNext)
        env = result.get('market_env', {}) or {}
        if env:
            sentiment = env.get('sentiment', {}) or {}
            eco = env.get('ecosystem', {}) or {}
            lines.append("🌤️【市场环境】")
            try:
                from core.utils import normalize_weather
                w_disp = normalize_weather(env.get('weather', '-'))
            except Exception:
                w_disp = env.get('weather','-')
            lines.append(f"  天气: {w_disp} | 风险: {display_risk_level(env.get('risk_level','-'))} | {humanize_text(env.get('message',''))}")
            lines.append(f"  涨停/跌停: {sentiment.get('limit_up',0)}/{sentiment.get('limit_down',0)}")
            if eco:
                height = eco.get('limit_up_height')
                ladder = eco.get('ladder_distribution') or {}
                if height is not None:
                    lines.append(f"  最高板: {height}")
                if ladder:
                    ladder_str = " ".join([f"{k}板:{v}" for k, v in ladder.items()])
                    lines.append(f"  梯队: {ladder_str}")
            # Concept heat (best-effort)
            ch = result.get('concept_heat') or {}
            top = ch.get('top') or []
            if top:
                top_str = " | ".join([f"{t.get('concept')}({t.get('count')})" for t in top[:8] if t.get('concept')])
                if top_str:
                    lines.append(f"  题材热度: {top_str}")
            lines.append("")

        self._append_today_focus(lines, result)

        # === KEY SECTION: Money Flow Picks ===
        hot = result.get('hot_stocks', [])
        count = len(hot)
        
        if hot:
            lines.append(f"🔥【资金流精选】(评分排序)")
            lines.append(f"| 代码 | 名称 | 评分 | 现价 | 涨幅 | 换手 | 成交(亿) | 板块(均涨) |")
            lines.append(f"|:---:|:---|---:|---:|---:|---:|---:|:---|")
            for s in hot[:10]:
                reason = s.get('reason', s.get('industry', ''))
                if s.get('first_board_tag'):
                    reason = f"{reason} | {s.get('first_board_tag')}"
                if s.get('lhb_present_today'):
                    reason = f"{reason} | LHB"
                lines.append(f"| {s['code']} | {s['name']} | {s.get('score', 0):.1f} | {s['price']:.2f} | {s['change']:+.2f}% | {s['turnover']:.2f}% | {s['amount']:.2f} | {reason} |")
            lines.append("")
            
            # Sector distribution summary
            sectors = {}
            for s in hot:
                ind = s.get('industry', '其他')
                if ind not in sectors: sectors[ind] = 0
                sectors[ind] += 1
            
            if sectors:
                lines.append("【板块分布】")
                sorted_secs = sorted(sectors.items(), key=lambda x: x[1], reverse=True)
                sec_strs = [f"{k}({v}只)" for k, v in sorted_secs[:5]]
                lines.append(f"  {' | '.join(sec_strs)}")
                lines.append("")
        else:
            lines.append("【资金流精选】暂无符合条件个股")
            lines.append("  (涨幅2-5% + 换手合理 + 强势板块 条件较严)")
            lines.append("")
        
        # Recommendation
        self._append_recommendation(lines, result)
        self._append_execution_audit(lines, result)
        
        # === DB Summary ===
        self._append_db_summary(lines, result, 'afternoon')
        
        return "\n".join(lines)
    
    # =========================================
    # POST-MARKET (16:00) - 收盘复盘 + 明日备选
    # =========================================
    def _format_post_market(self, result):
        lines = []
        date_str = datetime.now().strftime('%Y-%m-%d')
        
        lines.append("=" * 60)
        lines.append(f"🌙 收盘复盘报告 ({date_str})")
        lines.append("策略: 今日全市场资金流 → Top板块 → 正涨幅龙头股")
        lines.append("=" * 60)
        lines.append("")
        
        # Portfolio
        self._append_portfolio(lines, result)

        # Market environment (VNext)
        env = result.get('market_env', {}) or {}
        if env:
            sentiment = env.get('sentiment', {}) or {}
            eco = env.get('ecosystem', {}) or {}
            lines.append("🌤️【市场环境】")
            try:
                from core.utils import normalize_weather
                w_disp = normalize_weather(env.get('weather', '-'))
            except Exception:
                w_disp = env.get('weather','-')
            lines.append(f"  天气: {w_disp} | 风险: {display_risk_level(env.get('risk_level','-'))} | {humanize_text(env.get('message',''))}")
            lines.append(f"  涨停/跌停: {sentiment.get('limit_up',0)}/{sentiment.get('limit_down',0)}")
            if eco:
                height = eco.get('limit_up_height')
                ladder = eco.get('ladder_distribution') or {}
                if height is not None:
                    lines.append(f"  最高板: {height}")
                if ladder:
                    ladder_str = " ".join([f"{k}板:{v}" for k, v in ladder.items()])
                    lines.append(f"  梯队: {ladder_str}")
            # Concept heat (best-effort)
            ch = result.get('concept_heat') or {}
            top = ch.get('top') or []
            if top:
                top_str = " | ".join([f"{t.get('concept')}({t.get('count')})" for t in top[:8] if t.get('concept')])
                if top_str:
                    lines.append(f"  题材热度: {top_str}")
            lines.append("")

        self._append_today_focus(lines, result)

        dq = result.get('data_quality', {}) or {}
        if dq.get('moneyflow_date'):
            suffix = ""
            if dq.get('moneyflow_fallback'):
                suffix = f"（当日 {dq.get('moneyflow_preferred_date')} 暂无，已回退）"
            lines.append(f"📌【数据口径】资金流日期: {dq.get('moneyflow_date')}{suffix}")
            lines.append("")

        # === KEY SECTION: Tomorrow Picks ===
        hot = result.get('hot_stocks', [])
        count = len(hot)
        
        if hot:
            lines.append(f"⭐【明日备选股】共 {count} 只 (今日资金流入+收盘上涨)")
            lines.append(f"| 代码 | 名称 | 收盘价 | 涨幅 | 换手 | 成交(亿) | 板块 | 资金流入 |")
            lines.append(f"|:---:|:---|---:|---:|---:|---:|:---|:---|")
            for s in hot[:15]:
                reason = s.get('reason', '')
                if s.get('first_board_tag'):
                    reason = f"{reason} | {s.get('first_board_tag')}"
                industry = s.get('industry', '')
                lines.append(f"| {s['code']} | {s['name']} | {s['price']:.2f} | {s['change']:+.2f}% | {s['turnover']:.2f}% | {s['amount']:.2f} | {industry} | {reason} |")
            lines.append("")
            
            # Observation Notes
            lines.append("💡【观察要点】")
            lines.append("  1. 以上为今日资金净流入+收盘上涨的强势股")
            lines.append("  2. 明日开盘关注: 是否高开/平开, 集合竞价量能")
            lines.append("  3. 若高开2-3%, 可参考早盘竞价策略介入")
            lines.append("  4. 若低开, 观望为主, 等待午盘信号")
            lines.append("")
        else:
            lines.append("【明日备选股】暂无符合条件个股")
            stats = (dq.get('filter_stats') or {}) if isinstance(dq, dict) else {}
            if stats:
                lines.append(
                    "  (过滤统计: "
                    f"Top行业候选{int(stats.get('candidate_count', 0) or 0)}只, "
                    f"行情{int(stats.get('quote_count', 0) or 0)}只, "
                    f"涨幅通过{int(stats.get('pass_close_change', 0) or 0)}只, "
                    f"换手通过{int(stats.get('pass_turnover', 0) or 0)}只, "
                    f"基础门槛通过{int(stats.get('pass_pre_risk', 0) or 0)}只, "
                    f"过热/MACD后{int(stats.get('final_count', 0) or 0)}只)"
                )
            else:
                lines.append("  (今日整体偏弱或过滤条件下无候选)")
            lines.append("")
        
        # Recommendation
        self._append_recommendation(lines, result)
        self._append_execution_audit(lines, result)
        
        # === DB Summary ===
        self._append_db_summary(lines, result, 'post_market')
        
        return "\n".join(lines)
        
    # =========================================
    # WATCHLIST (10:00 - 14:00) 
    # =========================================
    def _format_watchlist(self, result):
        lines = []
        date_str = datetime.now().strftime('%Y-%m-%d %H:%M')
        
        lines.append("=" * 60)
        lines.append(f"🔭 备选池巡航报告 ({date_str})")
        lines.append("策略: 巡航近期备选池，突破MA5买入，跌破MA20剔除")
        lines.append("=" * 60)
        lines.append("")
        
        # Portfolio
        self._append_portfolio(lines, result)

        # Market environment (VNext)
        env = result.get('market_env', {}) or {}
        if env:
            sentiment = env.get('sentiment', {}) or {}
            eco = env.get('ecosystem', {}) or {}
            lines.append("🌤️【市场环境】")
            try:
                from core.utils import normalize_weather
                w_disp = normalize_weather(env.get('weather', '-'))
            except Exception:
                w_disp = env.get('weather','-')
            lines.append(f"  天气: {w_disp} | 风险: {display_risk_level(env.get('risk_level','-'))} | {humanize_text(env.get('message',''))}")
            lines.append(f"  涨停/跌停: {sentiment.get('limit_up',0)}/{sentiment.get('limit_down',0)}")
            if eco:
                height = eco.get('limit_up_height')
                ladder = eco.get('ladder_distribution') or {}
                if height is not None:
                    lines.append(f"  最高板: {height}")
                if ladder:
                    ladder_str = " ".join([f"{k}板:{v}" for k, v in ladder.items()])
                    lines.append(f"  梯队: {ladder_str}")
            # Concept heat (best-effort)
            ch = result.get('concept_heat') or {}
            top = ch.get('top') or []
            if top:
                top_str = " | ".join([f"{t.get('concept')}({t.get('count')})" for t in top[:8] if t.get('concept')])
                if top_str:
                    lines.append(f"  题材热度: {top_str}")
            lines.append("")

        self._append_today_focus(lines, result)

        wd = result.get('watchlist_data', {})
        buys = wd.get('buy_candidates', [])
        removed = wd.get('removed', [])
        expired = wd.get('expired', [])
        observed = wd.get('observed', [])
        
        if buys:
            lines.append(f"🟢【买入信号触发】共 {len(buys)} 只")
            lines.append(f"| 代码 | 名称 | 评分 | 来源 | 入选日 | 现价 | 涨幅 | 量比 | 模型 | 信号 |")
            lines.append(f"|:---:|:---|---:|:---|:---|---:|---:|---:|:---|:---|")
            for s in buys:
                signal = s.get('reason','')
                if s.get('board_context') == 'first_board':
                    signal = f"[首板]{signal}"
                model_cell = self._format_combo_model_cell(s)
                lines.append(f"| {s['code']} | {s['name']} | {s.get('score',0):.1f} | {s.get('strategy','未知')[:4]} | {s.get('date', '')[:10]} | {s.get('price',0):.2f} | {s.get('change',0):+.2f}% | {s.get('vol_ratio',0):.1f} | {model_cell} | {humanize_text(signal)} |")
            lines.append("")

        if expired:
            lines.append(f"⏱️【到期停止观察】共 {len(expired)} 只")
            lines.append(f"| 代码 | 名称 | 来源 | 入选日 | 已观察 | 入选价 | 现价 | 累计盈亏 | 原因 |")
            lines.append(f"|:---:|:---|:---|:---|---:|---:|---:|---:|:---|")
            for s in expired:
                reason = s.get('reason','')
                if s.get('board_context') == 'first_board':
                    reason = f"[首板]{reason}"
                lines.append(
                    f"| {s['code']} | {s['name']} | {s.get('strategy','未知')[:4]} | {s.get('date', '')[:10]} | "
                    f"{int(s.get('age_days', 0) or 0)}天 | {s.get('sel_price',0):.2f} | {s.get('price',0):.2f} | "
                    f"{s.get('total_chg',0):+.2f}% | {humanize_text(reason)} |"
                )
            lines.append("")
            
        if removed:
            lines.append(f"🔴【破位剔除】共 {len(removed)} 只")
            lines.append(f"| 代码 | 名称 | 来源 | 入选日 | 入选价 | 现价 | 累计盈亏 | 原因 |")
            lines.append(f"|:---:|:---|:---|:---|---:|---:|---:|:---|")
            for s in removed:
                reason = s.get('reason','')
                if s.get('board_context') == 'first_board':
                    reason = f"[首板]{reason}"
                lines.append(f"| {s['code']} | {s['name']} | {s.get('strategy','未知')[:4]} | {s.get('date', '')[:10]} | {s.get('sel_price',0):.2f} | {s.get('price',0):.2f} | {s.get('total_chg',0):+.2f}% | {humanize_text(reason)} |")
            lines.append("")
            
        lines.append(f"📈【潜伏观察中】共 {len(observed)} 只")
        if observed:
            lines.append(f"| 代码 | 名称 | 来源 | 入选日 | 入选价 | 现价 | 累计盈亏 | 模型 | 信号 |")
            lines.append(f"|:---:|:---|:---|:---|---:|---:|---:|:---|:---|")
            for s in observed:
                signal = s.get('reason','')
                if s.get('board_context') == 'first_board':
                    signal = f"[首板]{signal}"
                model_cell = self._format_combo_model_cell(s)
                lines.append(f"| {s['code']} | {s['name']} | {s.get('strategy','未知')[:4]} | {s.get('date', '')[:10]} | {s.get('sel_price',0):.2f} | {s.get('price',0):.2f} | {s.get('total_chg',0):+.2f}% | {model_cell} | {humanize_text(signal)} |")
        lines.append("")
        
        self._append_recommendation(lines, result)
        self._append_execution_audit(lines, result)
        return "\n".join(lines)

    def format_focus_monitor_report(self, snapshot):
        """Format all-day focus monitor report."""
        snapshot = snapshot or {}
        lines = []
        generated_at = snapshot.get('generated_at') or datetime.now().strftime('%Y-%m-%d %H:%M')
        summary = snapshot.get('summary') or {}
        offsets = snapshot.get('offsets') or {}

        lines.append("=" * 60)
        lines.append(f"🎯 全天重点雷达 ({generated_at})")
        lines.append("口径: 昨日入库 + 盘后T+2 + 近5日重点观测池；只写影子审计，不改买卖状态")
        lines.append("=" * 60)
        lines.append("")
        lines.append("🧭【监测范围】")
        lines.append(f"  今日: {self._fmt_trade_date(offsets.get('today'))}")
        lines.append(f"  T+1来源: {summary.get('t1_date') or self._fmt_trade_date(offsets.get('t1'))}")
        lines.append(f"  T+2来源: {summary.get('t2_date') or self._fmt_trade_date(offsets.get('t2'))}")
        lines.append(f"  总监测: {summary.get('total', 0)} 只 | 强盯/重点: {summary.get('strong', 0)} 只 | 风险降级: {summary.get('risk', 0)} 只")
        shadow = snapshot.get("shadow_pending") or {}
        if shadow.get("enabled"):
            strategies = "、".join(shadow.get("strategies") or []) or "-"
            lines.append(
                f"  影子审计: 写入 {shadow.get('written', 0)} 条 | "
                f"策略: {strategies} | 状态: {display_status(shadow.get('status', 'SHADOW'))} | 不进入真实买入队列"
            )
        lines.append("")

        self._append_focus_rows(lines, "🔥【最该盯 Top】", snapshot.get('top_focus') or [])
        self._append_focus_rows(lines, "📌【昨日/盘后入库表现】", snapshot.get('yesterday') or [])
        self._append_focus_rows(lines, "🔭【重点观测池】", snapshot.get('active_watch') or [])
        self._append_focus_rows(lines, "⚠️【风险降级】", snapshot.get('risk') or [])

        lines.append("🚦【交易与观察拆分】")
        lines.append("  • 本邮件只回答“今天该盯谁、为什么盯”，不代表自动买入已放行。")
        lines.append("  • 影子审计只记录重点票和失败/观察原因，状态=影子审计，不会被真实买入流程读取。")
        lines.append("  • 自动买入仍会受市场天气、板块轮动、入场确认、资金/仓位等门槛限制。")
        lines.append("  • 若重点票持续T+1/T+2表现好但不成交，下一轮应训练/审计的是入场门槛，不是选股本身。")
        lines.append("")
        lines.append("-" * 60)
        lines.append("注: 以上为grid自动生成，不构成投资建议")
        return "\n".join(lines)
    
    # =========================================
    # SHARED COMPONENTS
    # =========================================
    def _fmt_trade_date(self, value):
        value = str(value or "")
        value = value.replace("-", "")
        if len(value) == 8 and value.isdigit():
            return f"{value[:4]}-{value[4:6]}-{value[6:]}"
        return value or "-"

    def _append_focus_rows(self, lines, title, rows, limit=12):
        rows = list(rows or [])
        if not rows:
            lines.append(f"{title}：暂无")
            lines.append("")
            return
        lines.append(f"{title} 共 {len(rows)} 只")
        lines.append("| 级别 | 来源 | 周期 | 入库日 | 策略 | 代码 | 名称 | 入库价 | 现价 | 今日 | 相对入库 | 量比 | 60m标签 | 审计原因 | 动作 |")
        lines.append("|:---:|:---|:---:|:---:|:---|:---:|:---|---:|---:|---:|---:|---:|:---|:---|:---|")
        for r in rows[:limit]:
            label = self._format_focus_minute_label(r)
            audit_reason = self._format_focus_audit_reason(r)
            lines.append(
                f"| {r.get('level','-')} | {r.get('bucket','-')} | {r.get('cycle','-')} | {str(r.get('date',''))[:10]} | "
                f"{r.get('strategy','-')} | {r.get('code','-')} | {str(r.get('name','-'))[:8]} | "
                f"{self._safe_float(r.get('sel_price'), 0):.2f} | {self._safe_float(r.get('price'), 0):.2f} | "
                f"{self._safe_float(r.get('intraday_pct'), 0):+.2f}% | {self._safe_float(r.get('total_pct'), 0):+.2f}% | "
                f"{self._safe_float(r.get('vol_ratio'), 0):.1f} | {label} | {audit_reason} | {display_action(r.get('action','-'))} |"
            )
        if len(rows) > limit:
            lines.append(f"| ... | ... | ... | ... | ... | ... | 还有{len(rows)-limit}只 | ... | ... | ... | ... | ... | ... | ... | ... |")
        lines.append("")

    def _format_focus_minute_label(self, row):
        labels = row.get("minute_labels") or []
        if isinstance(labels, str):
            labels = [labels]
        labels = [str(x) for x in labels if x]
        if labels:
            return "、".join(labels[:2])
        if row.get("leader_hard_false_strength_risk_60m"):
            return "60m强冲高回落风险"
        if row.get("leader_false_strength_risk_60m"):
            return "60m冲高回落风险"
        if row.get("leader_strong_sustained_strength_60m"):
            return "60m强势延续"
        if row.get("leader_sustained_strength_watch_60m"):
            return "60m持续强势观察"
        if row.get("leader_close_hold_gate"):
            return "收盘持有闸门"
        return "-"

    def _format_focus_audit_reason(self, row):
        reason = str(row.get("failure_reason_primary") or "").strip()
        if not reason:
            return "-"
        mapping = {
            "PERMISSION_OBSERVE_ONLY": "权限观察",
            "PERMISSION_BLOCK": "权限阻断",
            "SOURCE_NOT_ROUTED": "来源未路由",
            "SCHEDULE_WINDOW_MISSING": "窗口缺失",
            "DATA_QUALITY_BAD": "数据质量",
            "SECTOR_GATE_REJECT": "行业拒绝",
            "PRICE_BAND_CHASE_RISK": "追高风险",
        }
        return mapping.get(reason, humanize_text(reason, 18))

    def _append_today_focus(self, lines, result):
        rows = list((result or {}).get('today_focus') or [])
        focus_snapshot = (result or {}).get('focus_monitor') or {}
        radar_rows = list(focus_snapshot.get('top_focus') or [])[:5]
        if not rows and not radar_rows:
            return

        lines.append("🎯【今日重点关注】")
        lines.append("  说明: 这里按“值得盯”的优先级排序，和自动买入是否放行是两回事。")
        if rows:
            lines.append("| 级别 | 来源 | 周期 | 代码 | 名称 | 价格 | 涨幅 | 换手 | 分数 | 60m标签 | 盯盘动作 | 原因 |")
            lines.append("|:---:|:---|:---:|:---:|:---|---:|---:|---:|---:|:---|:---|:---|")
            for r in rows[:8]:
                label = self._format_focus_minute_label(r)
                lines.append(
                    f"| {r.get('level','-')} | {r.get('source','-')} | {r.get('cycle','-')} | {r.get('code','-')} | "
                    f"{str(r.get('name','-'))[:8]} | {self._safe_float(r.get('price'), 0):.2f} | "
                    f"{self._safe_float(r.get('change'), 0):+.2f}% | {self._safe_float(r.get('turnover'), 0):.2f}% | "
                    f"{self._safe_float(r.get('score'), 0):.1f} | {label} | {display_action(r.get('action','-'))} | {humanize_text(r.get('reason','-'), 32)} |"
                )
        if radar_rows:
            lines.append("")
            lines.append("  📡 昨日/观测池雷达 Top:")
            for r in radar_rows:
                label = self._format_focus_minute_label(r)
                label_text = f"｜{label}" if label != "-" else ""
                lines.append(
                    f"  • {r.get('level','-')} {r.get('name','-')}({r.get('code','-')}) "
                    f"{r.get('strategy','-')} 入库{self._safe_float(r.get('sel_price'), 0):.2f}→现{self._safe_float(r.get('price'), 0):.2f} "
                    f"相对{self._safe_float(r.get('total_pct'), 0):+.1f}%{label_text}｜{display_action(r.get('action','-'))}"
                )
        lines.append("")

    def _format_combo_model_cell(self, item):
        """Format observe-mode combo scores for watchlist tables."""
        parts = []
        first_score = item.get("first_limit_score_model")
        breakout_score = item.get("breakout_confirm_score_model")
        false60_score = item.get("breakout_false60_score_model")
        net60_score = item.get("breakout_net60_score_model")
        cold_good = item.get("cold_start_good_score")
        cold_profit = item.get("cold_start_profit_score")
        cold_risk = item.get("cold_start_risk_score")
        cold_score = item.get("cold_start_observe_score") or item.get("cold_start_score_10m")
        entry_delay = item.get("recommended_entry_delay_min")
        combo_score = item.get("combo_score_product")
        execution_pass = item.get("breakout_execution_gate_pass")
        queue_risk = item.get("breakout_limit_queue_risk")

        if first_score is not None:
            parts.append(f"首{float(first_score):.2f}")
        if breakout_score is not None:
            parts.append(f"突{float(breakout_score):.2f}")
        if false60_score is not None:
            parts.append(f"假{float(false60_score):.2f}")
        if net60_score is not None:
            parts.append(f"净{float(net60_score):.2f}")
        if cold_good is not None:
            parts.append(f"冷好{float(cold_good):.2f}")
        if cold_profit is not None:
            parts.append(f"冷利{float(cold_profit):.2f}")
        if cold_risk is not None:
            parts.append(f"冷险{float(cold_risk):.2f}")
        if cold_score is not None:
            parts.append(f"冷分{float(cold_score):.2f}")
        if bool(item.get("cold_start_early_absorb")):
            parts.append("吸收")
        if bool(item.get("cold_start_delayed_confirm")):
            parts.append("承接")
        if bool(item.get("cold_start_pullback_entry_candidate")):
            window = item.get("cold_start_pullback_window_min")
            if window is not None:
                parts.append(f"低吸{int(window)}m")
            else:
                parts.append("低吸")
        if entry_delay is not None:
            parts.append(f"等{int(entry_delay)}m")
        if combo_score is not None:
            parts.append(f"组{float(combo_score):.2f}")
        if bool(item.get("combo_gate_pass", False)):
            parts.append("过")
        if bool(item.get("breakout_high_quality_gate_pass", False)):
            parts.append("强突")
        if execution_pass is not None:
            parts.append("可执" if bool(execution_pass) else "执险")
        if bool(queue_risk):
            parts.append("排队险")
        if parts:
            return " ".join(parts)
        return self._format_base_model_cell(item)

    def _format_base_model_cell(self, item):
        """Readable fallback when training/observe model fields are absent."""
        strategy = str(item.get('strategy') or '').strip()
        board_context = str(item.get('board_context') or '').strip()
        if item.get('combo_observe_mode') and not any(item.get(k) is not None for k in (
            'first_limit_score_model',
            'breakout_confirm_score_model',
            'combo_score_product',
        )):
            return '观察模型待样本'
        if board_context in ('first_board', '首板候选') or bool(item.get('is_first_board_candidate')):
            return '首板规则'
        if board_context in ('continue_board', '接力候选') or bool(item.get('is_continue_board_candidate')):
            return '接力规则'
        if strategy:
            if '冷启动' in strategy:
                return '冷启动规则'
            if '盘后' in strategy:
                return '盘后资金规则'
            if '午盘' in strategy:
                return '午盘资金规则'
            if '龙头' in strategy:
                return '龙头跟踪规则'
            if '集合竞价' in strategy:
                return '竞价规则'
        return '基础规则'

    def _format_candidate_tag(self, item, default='基础候选'):
        """Readable fallback for business tags."""
        if item.get('lhb_present_today'):
            return 'LHB'
        if item.get('zt_tag'):
            return str(item.get('zt_tag'))
        cold_tags = item.get('cold_start_model_tags') or []
        if cold_tags:
            mapping = {
                'COLD_START_MODEL_GOOD': '冷启动好',
                'COLD_START_PROFIT_CAPTURE': 'T0利润',
                'COLD_START_RISK_HIGH': '风险高',
                'COLD_START_EARLY_ABSORB': '早盘吸收',
                'COLD_START_DELAYED_CONFIRM': 'VWAP承接',
                'COLD_START_VWAP_SUPPORT_OBSERVE': 'VWAP承接',
                'COLD_START_PULLBACK_ENTRY_WATCH': '低吸观察',
            }
            return "/".join(mapping.get(str(tag), str(tag)) for tag in cold_tags[:4])
        if item.get('first_board_tag'):
            return str(item.get('first_board_tag'))
        if '冷启动' in str(item.get('strategy') or '') or any(k in item for k in ('cold_start_good_score', 'cold_start_risk_score', 'cold_start_observe_score')):
            return default
        board_context = str(item.get('board_context') or '').strip()
        if board_context in ('first_board', '首板候选') or bool(item.get('is_first_board_candidate')):
            return '普通首板'
        if board_context in ('continue_board', '接力候选') or bool(item.get('is_continue_board_candidate')):
            return '接力观察'
        return default

    def _append_portfolio(self, lines, result):
        """Append portfolio summary section for available accounts"""
        portfolios = []
        if 'portfolio_main' in result:
            portfolios.append(('【主账户资产】', result['portfolio_main']))
        if 'portfolio_watch' in result:
            portfolios.append(('【巡航子账户资产】', result['portfolio_watch']))
        if 'portfolio_rescue' in result:
            portfolios.append(('【T+0 自救账户资产】', result['portfolio_rescue']))
        if 'portfolio_paper_main' in result:
            portfolios.append(('【Paper 主账户仿真】', result['portfolio_paper_main']))
        if 'portfolio_paper_watchlist' in result:
            portfolios.append(('【Paper 巡航仿真】', result['portfolio_paper_watchlist']))
            
        # Fallback (for older mocks or tests)
        if not portfolios and 'portfolio' in result:
            portfolios.append(('【资产概览】', result['portfolio']))
            
        if not portfolios:
            return
            
        for title, p in portfolios:
            total_asset = p.get('total_asset', 0)
            cash = p.get('cash', 0)
            mv = p.get('market_value', 0)
            # Get initial capital from database (accounts table)
            if 'Paper 主' in title:
                acc_type = 'paper_main'
            elif 'Paper 巡航' in title:
                acc_type = 'paper_watchlist'
            elif '主账户' in title:
                acc_type = 'main'
            elif '巡航' in title:
                acc_type = 'watchlist'
            else:
                acc_type = 'rescue'
                
            from core.portfolio import PortfolioManager
            initial = PortfolioManager().get_initial_capital(acc_type)
            ret = (total_asset - initial) / initial * 100 if initial > 0 else 0
            
            lines.append(title)
            if acc_type == 'rescue':
                lines.append(f"被套市值: {mv:.2f} (特殊自救隔离区，不计独立现金余额)")
            elif acc_type.startswith('paper_'):
                lines.append(f"仿真总市值: {mv:.2f} (影子账户，不占用真实现金)")
            else:
                lines.append(f"总资产: {total_asset:.2f} (收益率: {ret:+.2f}%)")
                lines.append(f"现  金: {cash:.2f}   持仓市值: {mv:.2f}")
            
            if p.get('positions'):
                lines.append(f"| 代码 | 名称 | 持仓 | 现价 | 成本 | 盈亏 | 盈亏% |")
                lines.append(f"|:---:|:---|---:|---:|---:|---:|---:|")
                for pos in p['positions']:
                    display_name = pos['name']
                    if pos.get('account') == 'rescue':
                        display_name = f"🚨[T0自救]{display_name}"
                    elif str(pos.get('account') or '').startswith('paper_'):
                        display_name = f"[Paper]{display_name}"
                    lines.append(f"| {pos['code']} | {display_name} | {pos['quantity']} | {pos['current_price']:.2f} | {pos['buy_price']:.2f} | {pos['pnl']:.2f} | {pos['pnl_pct']:+.2f}% |")
            lines.append("")
            lines.append("=" * 60)
            lines.append("")
    
    def _append_recommendation(self, lines, result):
        """Append recommendation section"""
        rec = result.get('recommendation', {})
        if not rec:
            return
        
        lines.append("【操作建议】")
        lines.append(f"市场强度: {rec.get('market_power', '-')}")
        lines.append(f"建议仓位: {rec.get('position', '-')}")
        lines.append(f"策略建议: {rec.get('suggestion', '-')}")
        if rec.get('top3'):
            lines.append(f"重点关注: {', '.join(rec['top3'])}")
        lines.append("")

        self._append_training_snapshot_status(lines)

    def _append_training_snapshot_status(self, lines):
        """Append loaded observe-model status when a training snapshot is active."""
        try:
            from core.config import Config
            cfg = Config.STRATEGY.get('first_limit_breakout_combo', {}) if isinstance(Config.STRATEGY, dict) else {}
            variant = cfg.get('breakout_model_variant')
            if not variant:
                return
            mode = humanize_text(cfg.get('mode', 'observe'))
            net_thr = float(cfg.get('breakout_net60_score_min', 0.0) or 0.0)
            false_max = float(cfg.get('breakout_false60_score_max', 0.0) or 0.0)
            delay = int(cfg.get('breakout_high_quality_entry_delay_min', cfg.get('breakout_normal_entry_delay_min', 10)) or 0)
            exec_gate = bool(cfg.get('breakout_execution_gate_enabled', False))
            note = str(cfg.get('training_note') or '').strip()
            lines.append("🧪【训练观察链路】")
            lines.append(
                f"  突破模型: {variant} | 模式: {mode} | 净分>={net_thr:.3f} | 假突破<={false_max:.3f} | 等待: {delay}m | 执行门槛: {'开' if exec_gate else '关'}"
            )
            if note:
                lines.append(f"  训练备注: {note}")
            lines.append("")
        except Exception:
            return

    def _safe_float(self, value, default=0.0):
        try:
            if value is None or value == "":
                return default
            return float(value)
        except Exception:
            return default

    def _selection_price(self, item):
        item = item or {}
        for key in ("sel_price", "price", "close", "trade"):
            value = item.get(key)
            price = self._safe_float(value, 0.0)
            if price > 0:
                return price
        return 0.0

    def _selection_code(self, item):
        code = str((item or {}).get("code") or "")
        if code:
            return code
        ts_code = str((item or {}).get("ts_code") or "")
        return ts_code.split(".")[0] if ts_code else "-"

    def _selection_name(self, item):
        return str((item or {}).get("name") or "-")[:10]

    def _tracking_note(self, cycle):
        if str(cycle).upper() == "T+2":
            return "后两个交易日15:35验证(T-1开盘作入场价)"
        return "下一交易日15:35验证入库价表现"

    def _append_saved_selection_rows(self, lines, strategy_label, items, *, cycle="T+1", limit=8):
        """Show the exact stored reference prices that strategy_selection uses."""
        items = list(items or [])
        if not items:
            return
        lines.append(f"  📌 {strategy_label}入库明细:")
        lines.append("| 策略 | 代码 | 名称 | 入库价 | 周期 | 后续追踪 |")
        lines.append("|:---|:---:|:---|---:|:---:|:---|")
        note = self._tracking_note(cycle)
        for item in items[:limit]:
            lines.append(
                f"| {strategy_label} | {self._selection_code(item)} | {self._selection_name(item)} | "
                f"{self._selection_price(item):.2f} | {cycle} | {note} |"
            )
        if len(items) > limit:
            lines.append(f"| ... | ... | 还有{len(items) - limit}只 | ... | {cycle} | 邮件仅展示前{limit}只 |")

    def _extract_leader_picks(self, result):
        picks = result.get("leader_picks") or []
        if picks:
            return picks
        extracted = []
        lua = result.get("limit_up_analysis", {}) or {}
        for sector_info in lua.get("sectors", [])[:5]:
            hb = sector_info.get("highest_board") or {}
            ts_code = hb.get("ts_code") or ""
            if not ts_code:
                continue
            extracted.append({
                "code": ts_code.split(".")[0],
                "ts_code": ts_code,
                "name": hb.get("name", ""),
                "price": self._safe_float(hb.get("price", hb.get("close", 0)), 0.0),
                "industry": sector_info.get("sector", ""),
            })
        return extracted

    def _append_execution_audit(self, lines, result):
        audit = result.get("execution_audit") or []
        if not audit:
            return
        lines.append("🚦【自动交易执行说明】")
        for item in audit[:12]:
            status = item.get("status") or "INFO"
            name = item.get("name") or item.get("code") or "本次任务"
            reason = humanize_text(item.get("reason") or "-")
            strategy = item.get("strategy")
            prefix = "  •"
            if status in ("BLOCKED", "PAUSED", "REJECTED"):
                prefix = "  ⛔"
            elif status in ("BOUGHT", "PASS"):
                prefix = "  ✅"
            tail = f" [{strategy}]" if strategy else ""
            lines.append(f"{prefix} {name}{tail}: {reason}")
        if len(audit) > 12:
            lines.append(f"  • 另有 {len(audit) - 12} 条执行记录未展开")
        lines.append("")
    
    def _append_db_summary(self, lines, result, mode):
        """Append database write summary"""
        hot = result.get('hot_stocks', [])
        auc = result.get('auction_picks', [])
        cold = result.get('cold_start_picks', [])
        
        lines.append("-" * 60)
        lines.append("📦【数据入库摘要】")
        lines.append("  说明: 入库价=写入 strategy_selection.sel_price 的跟踪参考价，不代表已真实成交。")
        
        db_write = result.get('db_write', {}) or {}
        report_only = bool(result.get('report_only', False))

        def write_status(count, strategy_label):
            if report_only:
                return f"  👀 报告预览 {count} 只 [{strategy_label}] 候选，--monitor 模式未写入 strategy_selection 表"
            return f"  ✅ 已写入 {count} 只 [{strategy_label}] 候选 → strategy_selection 表"

        if mode == 'pre_market':
            # Pre-market saves auction_picks and leader_picks
            if auc:
                meta = db_write.get('集合竞价') or {}
                if meta.get('blocked'):
                    lines.append(f"  ⛔ 集合竞价入库被阻断: {humanize_text(meta.get('reason',''))}")
                else:
                    saved = meta.get('saved', len(auc))
                    lines.append(write_status(saved, "集合竞价"))
                    self._append_saved_selection_rows(lines, "集合竞价", auc, cycle="T+1")
            else:
                lines.append("  ⚠️ 无竞价候选入库 (无符合条件个股)")

            if cold:
                meta = db_write.get('冷启动') or {}
                if meta.get('blocked'):
                    lines.append(f"  ⛔ 冷启动入库被阻断: {humanize_text(meta.get('reason',''))}")
                else:
                    saved = meta.get('saved', len(cold))
                    lines.append(write_status(saved, "冷启动"))
                    self._append_saved_selection_rows(lines, "冷启动", cold, cycle="T+1")

            # [V16] Add Leader Picks summary
            leader_picks = self._extract_leader_picks(result)
            if leader_picks:
                meta = db_write.get('龙头跟踪') or {}
                if meta.get('blocked'):
                    lines.append(f"  ⛔ 龙头跟踪入库被阻断: {humanize_text(meta.get('reason',''))}")
                else:
                    saved = meta.get('saved', len(leader_picks))
                    lines.append(write_status(saved, "龙头跟踪"))
                    self._append_saved_selection_rows(lines, "龙头跟踪", leader_picks, cycle="T+1")
            
            if hot:
                technical = result.get('technical_picks') or hot[:5]
                meta = db_write.get('技术突破') or {}
                if meta.get('blocked'):
                    lines.append(f"  ⛔ 技术突破入库被阻断: {humanize_text(meta.get('reason',''))}")
                else:
                    saved = meta.get('saved', len(technical))
                    lines.append(write_status(saved, "技术突破"))
                    self._append_saved_selection_rows(lines, "技术突破", technical, cycle="T+1")
                
        elif mode == 'afternoon':
            meta = db_write.get('午盘精选') or {}
            if meta.get('blocked'):
                lines.append(f"  ⛔ 午盘精选入库被阻断: {humanize_text(meta.get('reason',''))}")
            elif hot:
                saved = meta.get('saved', len(hot))
                lines.append(write_status(saved, "午盘精选"))
                self._append_saved_selection_rows(lines, "午盘精选", hot, cycle="T+1")
            else:
                lines.append("  ⚠️ 无午盘候选入库 (无符合条件个股)")
                
        elif mode == 'post_market':
            meta = db_write.get('盘后资金流') or {}
            if meta.get('blocked'):
                lines.append(f"  ⛔ 盘后资金流入库被阻断: {humanize_text(meta.get('reason',''))}")
            elif hot:
                saved = meta.get('saved', len(hot))
                lines.append(write_status(saved, "盘后资金流"))
                self._append_saved_selection_rows(lines, "盘后资金流", hot, cycle="T+2")
            else:
                dq = result.get('data_quality', {}) or {}
                stats = dq.get('filter_stats') or {}
                if stats:
                    lines.append(
                        "  ⚠️ 无收盘候选入库 "
                        f"(候选{int(stats.get('candidate_count', 0) or 0)}只 → "
                        f"基础门槛{int(stats.get('pass_pre_risk', 0) or 0)}只 → "
                        f"最终{int(stats.get('final_count', 0) or 0)}只)"
                    )
                else:
                    lines.append("  ⚠️ 无收盘候选入库 (过滤条件下无符合个股)")
        
        lines.append("")
        lines.append("-" * 60)
        lines.append("注: 以上为grid自动生成，不构成投资建议")
        
        return lines
    
    # =========================================
    # BUY ALERT
    # =========================================
    def format_buy_alert(self, code, name, price, quantity, cost, strategy, cash_before, cash_after, account=None, weather=None, snapshot_id=None):
        """Format auto-buy notification"""
        lines = []
        date_str = datetime.now().strftime('%Y-%m-%d %H:%M')

        lines.append("=" * 60)
        title_prefix = "模拟仓买入执行通知" if _is_sim_account(account) else "自动买入执行通知"
        lines.append(f"🟢 {title_prefix} ({date_str})")
        lines.append("=" * 60)
        lines.append("")
        lines.append("【交易详情】")
        lines.append(f"  股票: {name} ({code})")
        lines.append(f"  方向: 买入")
        lines.append(f"  价格: {price:.2f}")
        lines.append(f"  数量: {quantity} 股")
        lines.append(f"  金额: {cost:.2f} 元 (含手续费)")
        lines.append(f"  策略: {strategy}")
        if account:
            lines.append(f"  账户: {display_account(account)}")
        if weather:
            lines.append(f"  市场天气: {weather}")
        if snapshot_id:
            lines.append(f"  快照编号: {snapshot_id}")
        lines.append("")
        lines.append("【账户变动】")
        lines.append(f"  买入前现金: {cash_before:.2f}")
        lines.append(f"  买入后现金: {cash_after:.2f}")
        lines.append(f"  本次消耗:   {cash_before - cash_after:.2f}")
        lines.append("")
        lines.append("📦【数据入库】")
        lines.append(f"  ✅ 持仓表: 新增/更新持仓 {code}")
        lines.append(f"  ✅ 交易流水表: 记录买入流水")
        lines.append(f"  ✅ 账户余额表: 更新现金余额")
        lines.append("")
        lines.append("💡【操作提示】")
        risk = getattr(Config, 'RISK_MANAGEMENT', {})
        lines.append(f"  • 止损线: {risk.get('STOP_LOSS', -0.05)*100:.0f}% (自动)")
        lines.append(f"  • 止盈线: {risk.get('TAKE_PROFIT', 0.08)*100:.0f}% (自动)")
        lines.append(f"  • 最长持仓: {risk.get('MAX_HOLD_DAYS', 5)} 天")
        lines.append("")
        lines.append("-" * 60)
        note_prefix = "模拟仓执行" if _is_sim_account(account) else "grid自动交易执行"
        lines.append(f"注: {note_prefix}，不构成投资建议")

        return "\n".join(lines)
    
    # =========================================
    # SELL ALERT
    # =========================================
    def format_sell_alert(self, code, name, price, quantity, pnl, pnl_pct, reason, cash_after, action=None, pct_to_sell=None, account=None, weather=None):
        """Format auto-sell notification"""
        lines = []
        date_str = datetime.now().strftime('%Y-%m-%d %H:%M')

        # Emoji based on P&L
        emoji = "🔴" if pnl < 0 else "🟢"
        status = "止损" if pnl < 0 else "止盈"

        is_partial = (action == 'SELL_LADDER') or (pct_to_sell is not None and pct_to_sell < 0.999)
        direction = "卖出 (部分平仓)" if is_partial else "卖出 (全仓)"

        lines.append("=" * 60)
        title_prefix = "模拟仓卖出执行通知" if _is_sim_account(account) else "自动卖出执行通知"
        lines.append(f"{emoji} {title_prefix} ({date_str})")
        lines.append("=" * 60)
        lines.append("")
        lines.append("【交易详情】")
        lines.append(f"  股票: {name} ({code})")
        lines.append(f"  方向: {direction}")
        if pct_to_sell is not None:
            lines.append(f"  平仓比例: {pct_to_sell*100:.0f}%")
        lines.append(f"  价格: {price:.2f}")
        lines.append(f"  数量: {quantity} 股")
        lines.append(f"  盈亏: {pnl:+.2f} 元 ({pnl_pct:+.2f}%)")
        lines.append(f"  类别: {status}")
        lines.append(f"  原因: {humanize_text(reason)}")
        if account:
            lines.append(f"  账户: {display_account(account)}")
        if weather:
            lines.append(f"  市场天气: {weather}")
        lines.append("")
        lines.append("【账户变动】")
        lines.append(f"  卖出后现金: {cash_after:.2f}")
        lines.append("")
        lines.append("📦【数据入库】")
        if is_partial:
            lines.append(f"  ✅ 持仓表: 更新持仓 {code} (部分减仓)")
        else:
            lines.append(f"  ✅ 持仓表: 删除持仓 {code}")
        lines.append(f"  ✅ 交易流水表: 记录卖出流水")
        lines.append(f"  ✅ 账户余额表: 更新现金余额")
        lines.append("")
        lines.append("-" * 60)
        note_prefix = "模拟仓执行" if _is_sim_account(account) else "grid自动交易执行"
        lines.append(f"注: {note_prefix}，不构成投资建议")

        return "\n".join(lines)

    def format_manual_watch_alert(self, *, code, name, event, price, buy_price, quantity, pnl_pct, pnl_amount, reason, account=None, weather=None, levels=None, volume_ratio=None):
        """Format alert-only notification for externally held manual positions."""
        lines = []
        date_str = datetime.now().strftime('%Y-%m-%d %H:%M')
        levels = levels or {}

        lines.append("=" * 60)
        lines.append(f"🔔 手工持仓跟踪提醒 ({date_str})")
        lines.append("=" * 60)
        lines.append("")
        lines.append("【触发事件】")
        lines.append(f"  类型: {event}")
        lines.append(f"  原因: {humanize_text(reason)}")
        lines.append("")
        lines.append("【持仓快照】")
        lines.append(f"  股票: {name} ({code})")
        lines.append(f"  账户: {display_account(account) if account else '手工跟踪'}")
        lines.append(f"  现价: {price:.2f}")
        lines.append(f"  成本: {buy_price:.2f}")
        lines.append(f"  数量: {quantity} 股")
        lines.append(f"  浮动盈亏: {pnl_amount:+.2f} 元 ({pnl_pct:+.2f}%)")
        if weather:
            lines.append(f"  市场天气: {weather}")
        if volume_ratio is not None:
            try:
                lines.append(f"  量能: {float(volume_ratio):.2f} 倍近5日均量")
            except Exception:
                pass
        lines.append("")
        if levels:
            lines.append("【关键价位】")
            for label, value in levels.items():
                try:
                    if value is not None and float(value) > 0:
                        lines.append(f"  {label}: {float(value):.2f}")
                except Exception:
                    continue
            lines.append("")
        lines.append("【执行边界】")
        lines.append("  这是一条提醒，不会自动操作你的真实券商账户。")
        lines.append("  需要你结合盘口、分时量能和个人风险承受能力手动处理。")
        lines.append("")
        lines.append("-" * 60)
        lines.append("注: alert-only 手工持仓哨兵，不构成投资建议")

        return "\n".join(lines)
    
    # =========================================
    # STRATEGY TRACKER (Daily Check)
    # =========================================
    def format_tracker_report(self, check_result):
        """Format strategy tracker daily report"""
        lines = []
        date_str = datetime.now().strftime('%Y-%m-%d')
        
        if not check_result:
            lines.append("=" * 60)
            lines.append(f"📊 策略追踪日报 ({date_str})")
            lines.append("=" * 60)
            lines.append("")
            lines.append("⚠️ 今日无策略追踪数据 (昨日可能无选股记录)")
            lines.append("")
            lines.append("-" * 60)
            return "\n".join(lines)
        
        check_date = check_result.get('check_date', 'Unknown')
        stats = check_result.get('stats', {})
        details = check_result.get('details', [])
        config = check_result.get('config', {})
        
        lines.append("=" * 60)
        lines.append(f"📊 策略追踪日报 ({date_str})")
        t1_dates = sorted({str(s.get('_check_date', ''))[:10] for s in details if str(s.get('analysis_cycle', 'T+1')).upper() == 'T+1' and s.get('_check_date')})
        t2_dates = sorted({str(s.get('_check_date', ''))[:10] for s in details if str(s.get('analysis_cycle', '')).upper() == 'T+2' and s.get('_check_date')})
        lines.append(f"验证日期: {check_date} 的选股表现")
        if t1_dates or t2_dates:
            parts = []
            if t1_dates:
                parts.append(f"T+1验证 {'/'.join(t1_dates)} 入库票")
            if t2_dates:
                parts.append(f"T+2验证 {'/'.join(t2_dates)} 盘后票")
            lines.append("本次覆盖: " + "；".join(parts))
        lines.append("\n🧭【口径说明】")
        lines.append("  • T+1 实战组：昨天入库 → 以入库价(入选价)为买入参考，统计今天的冲高/收盘")
        lines.append("  • T+2 复盘组：前天18:00盘后入库 → 次日(昨天)盘中择机入场 → 统计今天的冲高/收盘")
        lines.append("  • T+2 入场价：默认取次日(T-1)开盘价作为保守 proxy；后续可升级为真实成交价")
        lines.append("=" * 60)
        lines.append("")
        
        # === KEY: Performance Summary ===
        all_stats = stats
        total_selected = sum(s.get('total', 0) for s in all_stats.values())
        total_success = sum(s.get('success_count', s.get('zt_count', 0)) for s in all_stats.values())
        
        lines.append("🎯【综合实战性能验证】")
        lines.append(f"  总选中: {total_selected} 只 (T+1/T+2 混合统计)")
        lines.append(f"  吃肉/涨停: {total_success} 只 (口径: 当日最大冲高 > 2% 即达标)")
        lines.append("")

        if details:
            lines.append("📌【今日验证清单】")
            lines.append("| 周期 | 入库日 | 策略 | 代码 | 名称 | 入库/入场价 | 最高价 | 收盘价 | 冲高 | 收盘 | 结果 |")
            lines.append("|:---:|:---:|:---|:---:|:---|---:|---:|---:|---:|---:|:---|")
            for s in details[:30]:
                cycle = str(s.get('analysis_cycle', 'T+1') or 'T+1').upper()
                entry_date = str(s.get('_check_date') or s.get('date') or '')[:10]
                strat = str(s.get('strategy', '未知'))[:6]
                price = float(s.get('buy_price') or s.get('sel_price') or s.get('price') or 0)
                high = float(s.get('t1_high') or 0)
                close = float(s.get('t1_close') or 0)
                max_ret = float(s.get('max_t1_return', 0) or 0) * 100
                close_ret = float(s.get('close_t1_return', 0) or 0) * 100
                lines.append(
                    f"| {cycle} | {entry_date} | {strat} | {s.get('code','-')} | {s.get('name','-')} | "
                    f"{price:.2f} | {high:.2f} | {close:.2f} | {max_ret:+.2f}% | {close_ret:+.2f}% | {s.get('result','-')} |"
                )
            if len(details) > 30:
                lines.append(f"| ... | ... | ... | ... | 另有{len(details) - 30}只 | ... | ... | ... | ... | ... | 详见下方分策略明细 |")
            lines.append("")
        
        # Dictionary for Display Name Mapping (Internal -> UI)
        name_map = {
            '集合竞价': '集合竞价',
            '龙头跟踪': '龙头跟踪',
            '技术突破': '技术突破',
            '盘后资金流': '盘后资金流',
            '午盘精选': '午盘精选'
        }
        
        # Display Order requested by user
        display_order = ['集合竞价', '龙头跟踪', '技术突破', '盘后资金流', '午盘精选']
        
        lines.append("📊【分策略战绩动态汇总】")
        t1_order = ['集合竞价', '龙头跟踪', '技术突破', '午盘精选']
        t2_order = ['盘后资金流']
        
        lines.append("🔹 T+1 实战组 (当日选/当日买):")
        for strat_name_raw in t1_order:
            if strat_name_raw not in all_stats: continue
            s_stats = all_stats[strat_name_raw]
            strat_name = name_map.get(strat_name_raw, strat_name_raw)
            rate = s_stats.get('success_rate', 0)
            success_ct = s_stats.get('success_count', s_stats.get('zt_count', 0))
            grade = "🔥" if rate >= 30 else ("✅" if rate >= 15 else "📈")
            lines.append(f"  • {strat_name}: {s_stats.get('total',0)}只 → {success_ct}吃肉 (胜率{rate:.1f}%) {grade}")
            
        lines.append("🔹 T+2 复盘组 (盘后选/次日买):")
        for strat_name_raw in t2_order:
            if strat_name_raw not in all_stats: 
                lines.append(f"  • {name_map.get(strat_name_raw, strat_name_raw)}: 暂无验证数据")
                continue
            s_stats = all_stats[strat_name_raw]
            strat_name = name_map.get(strat_name_raw, strat_name_raw)
            rate = s_stats.get('success_rate', 0)
            success_ct = s_stats.get('success_count', s_stats.get('zt_count', 0))
            grade = "🔥" if rate >= 30 else ("✅" if rate >= 15 else "📈")
            lines.append(f"  • {strat_name}: {s_stats.get('total',0)}只 → {success_ct}吃肉 (胜率{rate:.1f}%) {grade}")
        lines.append("")
        
        # === Individual Stock Results ===
        if details:
            # Group by strategy
            strategy_groups = {}
            # Ensure all_stats keys are present in strategy_groups, even if no details for them
            # This ensures all strategies from the summary also appear in the details section header
            # Initialize strategy_groups with all strategies found in the summary stats
            # The 'stats' variable already holds the summary for all strategies
            for strat_name_raw in stats.keys():
                strategy_groups[strat_name_raw] = []

            for s in details:
                strat = s.get('strategy', '未知')
                # Append to the list for the strategy, ensuring the key exists
                if strat not in strategy_groups:
                    strategy_groups[strat] = [] # Fallback if a strategy appears in details but not in summary stats
                strategy_groups[strat].append(s)
            
            # Display mapping and order
            name_map = {}
            t1_order = ['集合竞价', '龙头跟踪', '技术突破', '午盘精选']
            t2_order = ['盘后资金流']
            
            lines.append("------------------------------------------------------------")
            lines.append("📈【T+1 组选股详情】(昨日选取)")
            for strat_name_raw in t1_order:
                if strat_name_raw not in strategy_groups: continue
                stocks = strategy_groups[strat_name_raw]
                if not stocks: continue
                strat_name = name_map.get(strat_name_raw, strat_name_raw)
                lines.append(f"📋【{strat_name}】({len(stocks)} 只)")
                lines.append(f"| 代码 | 名称 | 入选时间 | 入选价 | 最大冲高 | 最终收盘 | 结果 |")
                lines.append(f"|:---:|:---|:---|---:|---:|---:|:---|")
                
                if not stocks:
                    lines.append(f"| - | - | 暂无验证数据 | - | - | - | {t_mode} 周期未到或无选股 |")
                    lines.append("")
                    continue
                for s in stocks:
                    res = s.get('result', 'N/A')
                    if res == '涨停': result_emoji = "🚀"
                    elif res == '吃肉': result_emoji = "🥩"
                    elif res == '震荡': result_emoji = "⚖️"
                    else: result_emoji = "🍜"
                    
                    sel_price_raw = s.get('sel_price')
                    if sel_price_raw is None:
                        sel_price_raw = s.get('price', 0)
                    sel_price = float(sel_price_raw or 0)
                    
                    max_t1_return = float(s.get('max_t1_return', 0)) * 100
                    close_t1_return = float(s.get('close_t1_return', 0)) * 100
                    
                    created_at = s.get('created_at', '')
                    entry_date = s.get('_check_date', '') # V17 Entry Date
                    time_str = ""
                    if hasattr(created_at, 'strftime'):
                        time_str = created_at.strftime("%H:%M")
                    elif isinstance(created_at, str) and len(created_at) >= 16:
                        time_str = created_at[11:16]
                    
                    # If entry date is T-2, label it clearly
                    display_time = f"{entry_date[5:]} {time_str}" if entry_date else time_str
                    
                    lines.append(f"| {s['code']} | {s['name']} | {display_time} | {sel_price:.2f} | {max_t1_return:+.2f}% | {close_t1_return:+.2f}% | {result_emoji}{res} |")
                lines.append("")

            lines.append("------------------------------------------------------------")
            lines.append("🌙【T+2 组复盘详情】(前日选取)")
            for strat_name_raw in t2_order:
                if strat_name_raw not in strategy_groups or not strategy_groups[strat_name_raw]:
                    lines.append(f"📋【{name_map.get(strat_name_raw, strat_name_raw)}】(无持仓周期到期)")
                    continue
                stocks = strategy_groups[strat_name_raw]
                strat_name = name_map.get(strat_name_raw, strat_name_raw)
                lines.append(f"📋【{strat_name}】({len(stocks)} 只)")
                lines.append(f"| 代码 | 名称 | 入选日期 | 入场价(T-1开盘) | 最大冲高 | 最终收盘 | 结果 |")
                lines.append(f"|:---:|:---|:---|---:|---:|---:|:---|")
                for s in stocks:
                    res = s.get('result', 'N/A')
                    result_emoji = "🚀" if res == '涨停' else ("🥩" if res == '吃肉' else ("⚖️" if res == '震荡' else "🍜"))
                    buy_price = float(s.get('buy_price') or s.get('sel_price') or s.get('price', 0) or 0)
                    max_t1_return = float(s.get('max_t1_return', 0)) * 100
                    close_t1_return = float(s.get('close_t1_return', 0)) * 100
                    display_time = s.get('_check_date', '')[5:] # MM-DD
                    lines.append(f"| {s['code']} | {s['name']} | {display_time} | {buy_price:.2f} | {max_t1_return:+.2f}% | {close_t1_return:+.2f}% | {result_emoji}{res} |")
                lines.append("")
        
        # === Observation-only: first-board vs non-first-board comparison ===
        try:
            cmp_rows = check_result.get('first_board_comparison') or []
            if cmp_rows:
                lines.append("⚖️【首板 vs 非首板 对比回测】(观测，不改交易)")
                lines.append("-" * 60)
                lines.append("| 分组 | 样本 | 胜率 | 平均冲高 | 平均收盘 |")
                lines.append("|:---|---:|---:|---:|---:|")
                for s in cmp_rows:
                    lines.append(
                        f"| {s.get('label','-')} | {int(s.get('cnt',0) or 0)} | {float(s.get('win_rate',0) or 0)*100:.1f}% | {float(s.get('avg_max_ret',0) or 0)*100:+.2f}% | {float(s.get('avg_close_ret',0) or 0)*100:+.2f}% |"
                    )
                lines.append("")
        except Exception:
            pass

        # === Observation-only: first-board tag effectiveness ===
        try:
            tag_stats = check_result.get('tag_stats') or []
            first_board_tags = [
                s for s in tag_stats
                if str(s.get('tag', '')).startswith('FIRST_BOARD')
            ]
            if first_board_tags:
                lines.append("🥇【首板标签效果验证】(观测，不改交易)")
                lines.append("-" * 60)
                lines.append("| 标签 | 样本 | 胜率 | 平均冲高 | 平均收盘 | 最佳冲高 | 最差收盘 |")
                lines.append("|:---|---:|---:|---:|---:|---:|---:|")
                for s in sorted(first_board_tags, key=lambda x: int(x.get('cnt', 0) or 0), reverse=True):
                    lines.append(
                        f"| {s.get('tag','-')} | {int(s.get('cnt',0) or 0)} | {float(s.get('win_rate',0) or 0)*100:.1f}% | {float(s.get('avg_max_ret',0) or 0)*100:+.2f}% | {float(s.get('avg_close_ret',0) or 0)*100:+.2f}% | {float(s.get('max_max_ret',0) or 0)*100:+.2f}% | {float(s.get('min_close_ret',0) or 0)*100:+.2f}% |"
                    )
                lines.append("")
        except Exception:
            pass

        # === Observation-only: training model tag effectiveness ===
        try:
            tag_stats = check_result.get('tag_stats') or []
            model_tag_names = {
                'FIRST_LIMIT_MODEL',
                'BREAKOUT_CONFIRM_MODEL',
                'BREAKOUT_FALSE60_MODEL',
                'BREAKOUT_NET60_MODEL',
                'BREAKOUT_FALSE60_VETO_PASS',
                'BREAKOUT_FALSE60_RISK_HIGH',
                'BREAKOUT_NET60_PASS',
                'BREAKOUT_HIGH_QUALITY',
                'ENTRY_DELAY_0M',
                'ENTRY_DELAY_10M',
                'FIRST_LIMIT_BREAKOUT_COMBO',
                'FIRST_LIMIT_BREAKOUT_PASS',
            }
            model_tags = [s for s in tag_stats if str(s.get('tag', '')) in model_tag_names]
            if model_tags:
                lines.append("🧪【训练模型标签效果验证】(观测，不改交易)")
                lines.append("-" * 60)
                lines.append("| 标签 | 样本 | 胜率 | 平均冲高 | 平均收盘 | 最佳冲高 | 最差收盘 |")
                lines.append("|:---|---:|---:|---:|---:|---:|---:|")
                for s in sorted(model_tags, key=lambda x: (int(x.get('cnt', 0) or 0), str(x.get('tag', ''))), reverse=True):
                    lines.append(
                        f"| {s.get('tag','-')} | {int(s.get('cnt',0) or 0)} | {float(s.get('win_rate',0) or 0)*100:.1f}% | {float(s.get('avg_max_ret',0) or 0)*100:+.2f}% | {float(s.get('avg_close_ret',0) or 0)*100:+.2f}% | {float(s.get('max_max_ret',0) or 0)*100:+.2f}% | {float(s.get('min_close_ret',0) or 0)*100:+.2f}% |"
                    )
                lines.append("")
        except Exception:
            pass

        # === Observation-only: time bucket × weather ===
        try:
            tb_stats = check_result.get('time_bucket_weather_stats') or []
            if tb_stats:
                lines.append("⏱️【进攻窗口验证(时间段×天气)】(观测，不改交易)")
                lines.append("-" * 60)
                lines.append("| 天气 | 时段 | 样本 | 胜率 | 平均冲高 | 平均收盘 | P5收盘(尾部) |")
                lines.append("|:---:|:---:|---:|---:|---:|---:|---:|")

                # Sort: weather then bucket
                def _k(x):
                    return (str(x.get('weather','')), str(x.get('time_bucket','')))

                for s in sorted(tb_stats, key=_k):
                    weather = s.get('weather', '-')
                    label = s.get('bucket_label') or s.get('time_bucket') or '-'
                    cnt = int(s.get('cnt', 0) or 0)
                    win_rate = float(s.get('win_rate', 0.0) or 0.0) * 100
                    avg_max = float(s.get('avg_max_ret', 0.0) or 0.0) * 100
                    avg_close = float(s.get('avg_close_ret', 0.0) or 0.0) * 100
                    p5_close = float(s.get('p5_close_ret', 0.0) or 0.0) * 100
                    lines.append(f"| {weather} | {label} | {cnt} | {win_rate:.1f}% | {avg_max:+.2f}% | {avg_close:+.2f}% | {p5_close:+.2f}% |")
                lines.append("")
        except Exception:
            pass

        # === Lifecycle Tracking [V9] ===
        lifecycle = check_result.get('lifecycle', {})
        if lifecycle:
            lines.append("🔭【全景生命周期追踪】(近5日备选池)")
            lines.append("-" * 60)
            
            # 1. 潜伏监控中
            obs = lifecycle.get('observing', [])
            lines.append(f"📈 潜伏监控中 ({len(obs)} 只等待买点表)")
            if obs:
                lines.append(f"| 代码 | 名称 | 来源 | 状态 | 入库价 | 现价 | 浮动盈亏 | 入库日期 | 当前原因 |")
                lines.append(f"|:---:|:---|:---|:---:|---:|---:|---:|:---|:---|")
                for s in obs:
                    sel_price = float(s.get('sel_price') or s.get('price', 0))
                    curr_price = float(s.get('current_price', sel_price))
                    
                    if sel_price > 0:
                        chg = (curr_price - sel_price) / sel_price * 100
                    else:
                        chg = 0.0
                        
                    date_str = str(s.get('date', ''))[:10]
                    status_label = {
                        'ACTIVE': '新入库',
                        'WATCHING': '观察',
                        'PENDING': '待入场',
                    }.get(str(s.get('observe_status') or '').upper(), s.get('zt_result', '-'))
                    lines.append(f"| {s['code']} | {s['name']} | {s.get('strategy','未知')[:4]} | {status_label} | {sel_price:.2f} | {curr_price:.2f} | {chg:+.2f}% | {date_str} | {humanize_text(s.get('observe_reason',''))[:24]} |")
            lines.append("")
                
            # 2. 成功入仓段
            bought = lifecycle.get('bought', [])
            lines.append(f"🟢 成功入仓段 ({len(bought)} 只已击发表)")
            if bought:
                lines.append(f"| 代码 | 名称 | 策略 | 持仓成本 | 入库日期 |")
                lines.append(f"|:---:|:---|:---|---:|:---|")
                for s in bought:
                    cost_str = f"{float(s['pos_cost']):.2f}" if 'pos_cost' in s else "未知"
                    date_str = str(s.get('date', ''))[:10]
                    lines.append(f"| {s['code']} | {s['name']} | {s.get('strategy','')[:6]} | {cost_str} | {date_str} |")
            lines.append("")
                    
            # 3. 破位剔除段
            removed = lifecycle.get('removed', [])
            lines.append(f"🔴 停止观察段 ({len(removed)} 只已放弃表)")
            if removed:
                lines.append(f"| 代码 | 名称 | 来源 | 状态 | 入库价 | 入库日期 | 停止原因 |")
                lines.append(f"|:---:|:---|:---|:---:|---:|:---|:---|")
                for s in removed:
                    sel_price = float(s.get('sel_price') or 0)
                    date_str = str(s.get('date', ''))[:10]
                    status_label = '到期' if str(s.get('observe_status') or '').upper() == 'EXPIRED' else '剔除'
                    reason = s.get('observe_end_reason') or s.get('observe_reason') or s.get('zt_result') or ''
                    lines.append(f"| {s['code']} | {s['name']} | {s.get('strategy','未知')[:4]} | {status_label} | {sel_price:.2f} | {date_str} | {humanize_text(reason)[:28]} |")
            lines.append("")
            lines.append("=" * 60)
            lines.append("")
        
        # === Current Strategy Parameters ===
        if config:
            lines.append("⚙️【当前策略参数】")
            auc = config.get('auction', {})
            aft = config.get('afternoon', {})
            if auc:
                lines.append(f"  竞价策略: 高开 {auc.get('min_open_change')}-{auc.get('max_open_change')}% | 换手>{auc.get('min_turnover')}%")
            if aft:
                lines.append(f"  午盘策略: 涨幅 {aft.get('min_change')}-{aft.get('max_change')}% | 换手 {aft.get('min_turnover')}-{aft.get('max_turnover')}%")
            lines.append("")
        
        # === DB Summary ===
        lines.append("📦【数据入库】")
        lines.append(f"  ✅ strategy_selection 表: 更新 {total_selected} 条验证结果 (涨停/未涨停)")
        lines.append(f"  ✅ strategy_stats 表: 写入 {check_date} 统计数据")
        lines.append(f"  ✅ strategy_performance.json: 导出反馈数据 (供策略进化)")
        lines.append("")
        lines.append("-" * 60)
        lines.append("注: 以上为grid自动生成的策略追踪报告")
        
        return "\n".join(lines)
    
    # =========================================
    # INDIVIDUAL STOCK ANALYSIS (V2)
    # =========================================
    def generate_stock_report_v2(self, stock_basic, quote, tech_advice, money_flow_str="数据缺失", buy_price=None):
        """
        [V2] Generate enhanced individual stock analysis report
        Args:
            stock_basic: dict with name, ts_code, industry, area, etc.
            quote: dict with close, pct_chg, turnover_rate, vol, amount, open, high, low, pre_close
            tech_advice: dict from TechAnalyzer.get_actionable_advice()
            money_flow_str: string describing net money flow or HSGT data
            buy_price: float, optional user's buy price
        """
        lines = []
        now_str = datetime.now().strftime('%Y-%m-%d')
        
        name = stock_basic.get('name', '未知')
        code = stock_basic.get('ts_code', '未知')
        industry = stock_basic.get('industry', '未知')
        
        # Ensure we have floats for quote data
        try: close = float(quote.get('price', quote.get('close', 0)))
        except: close = 0.0
        try: pre_close = float(quote.get('pre_close', 0))
        except: pre_close = 0.0
        
        # Calculate real change if pct_chg is missing from real-time quote
        try: 
            if 'pct_chg' in quote:
                pchg = float(quote['pct_chg'])
            else:
                pchg = (close - pre_close) / pre_close * 100 if pre_close > 0 else 0.0
        except: pchg = 0.0
        
        try: turnover = float(quote.get('turnover_rate', quote.get('turnover', 0)))
        except: turnover = 0.0
        
        try: amount = float(quote.get('amount', 0)) / 100000000 # Convert to hundred million (Yi) based on standard amount scale
        except: amount = 0.0
            
        change_amount = close - pre_close
        
        lines.append(f"## 📊 {code[:6]} {name} 深度诊断分析 (V2)")
        lines.append("")
        
        # 1. Dashboard
        lines.append("### 一、核心概况与行情")
        lines.append("| 行情指标 | 数值 | 基本面信息 | 内容 |")
        lines.append("|---|---|---|---|")
        
        trend_state = tech_advice.get('action', '未知').split('/')[0].strip()
        lines.append(f"| **最新价** | ¥{close:.2f} ({pchg:+.2f}%) | **行业板块** | {industry} |")
        lines.append(f"| **换手率** | {turnover:.2f}% | **地域板块** | {stock_basic.get('area', '未知')} |")
        lines.append(f"| **成交额** | {amount:.2f}亿 | **主力动向** | {money_flow_str} |")
        lines.append(f"| **技术状态**| `{trend_state}` | **上市板块** | {stock_basic.get('market', '未知')} |")
        lines.append("")
        
        # NEW SECTION: Position Diagnosis if buy_price is provided
        sl = tech_advice.get('stop_loss', 0)
        sup = tech_advice.get('support', 0)
        res = tech_advice.get('resistance', 0)
        
        if buy_price and buy_price > 0:
            lines.append("### 二、当前持仓考量")
            profit_loss = (close - buy_price) / buy_price * 100
            diff_to_stop = (close - sl) / close * 100
            profit_str = f"**{profit_loss:+.2f}%**" if profit_loss > 0 else f"**{profit_loss:+.2f}%**"
            
            lines.append(f"- **建仓成本**: ¥{buy_price:.2f}")
            lines.append(f"- **当前浮盈**: {profit_str} (每股盈亏: ¥{close - buy_price:+.2f})")
            
            if profit_loss < 0:
                if close < sl:
                    lines.append("- 🚨 **警报**: 股价已**跌破量化止损位**，**建议立即无条件离场止损**，保住本金！")
                elif diff_to_stop < 2:
                    lines.append(f"- ⚠️ **警报**: 正在逼近止损位 ¥{sl:.2f} (仅差 {diff_to_stop:.1f}%)，一旦跌破必须出局。")
                else:
                    lines.append(f"- 💡 **建议**: 当前处于浮亏状态，但仍在止损线之上 (距离止损 {diff_to_stop:.1f}%)，可继续耐心持有。")
            else:
                if close > res:
                    lines.append("- ✨ **建议**: 股价已突破近期阻力位，动能强劲，可继续向上看高一线，止盈位可上移。")
                else:
                    dist_to_res = (res - close) / close * 100
                    lines.append(f"- 💡 **建议**: 处于盈利状态，距离上方阻力位 ¥{res:.2f} 还有约 {dist_to_res:.1f}% 空间，持股待涨。")
            lines.append("")
        
        # 2. Quant Scan
        idx = "三" if buy_price and buy_price > 0 else "二"
        lines.append(f"### {idx}、纯技术面量化扫描")
        lines.append(f"- **波动率 (ATR)**: 日均波幅约 ¥{tech_advice.get('atr', 0):.2f}")
        lines.append(f"- **关键防守位**: 强支撑 ¥{sup:.2f} / **量化止损点 ¥{sl:.2f}** (跌破无条件离场)")
        lines.append(f"- **上行阻力位**: ¥{res:.2f}")
        lines.append("")
                
        # 4. Actionable Advice
        idx2 = "四" if buy_price and buy_price > 0 else "三"
        lines.append(f"### {idx2}、🤖 交易系统策略执行建议")
        lines.append(f"- **当前操作**: **{tech_advice.get('action', '观望')}**")
        lines.append(f"- **入场条件**: {tech_advice.get('entry_condition', '等待明确信号')}")
        lines.append(f"- **最大风控额度**: 建议最高仓位不超过总资金 **{tech_advice.get('max_position', '5%')}**")
        lines.append("")
        lines.append("*注: 诊断数据基于纯量化技术模型自动生成，仅供实战战术参考。*")
        
        return "\n".join(lines)

    # =========================================
    # INDIVIDUAL STOCK ANALYSIS (V3 Holographic)
    # =========================================
    def generate_stock_report_v3(self, stock_basic, quote, tech_advice, money_flow_str="数据缺失", buy_price=None, v3_data=None, position_context=None):
        """
        [V3] Generate holographic individual stock analysis report (includes fundamentals, concepts, margin)
        """
        if v3_data is None: v3_data = {}
        if position_context is None: position_context = {}
        fina = v3_data.get('fina') or {}
        surv = v3_data.get('surv') or {}
        concepts = v3_data.get('concepts') or []
        margin = v3_data.get('margin') or {}
        
        lines = []
        now_str = datetime.now().strftime('%Y-%m-%d')
        
        name = stock_basic.get('name', '未知')
        code = stock_basic.get('ts_code', '未知')
        industry = stock_basic.get('industry', '未知')
        
        # Ensure we have floats for quote data
        try: close = float(quote.get('price', quote.get('close', 0)))
        except: close = 0.0
        try: pre_close = float(quote.get('pre_close', 0))
        except: pre_close = 0.0
        
        try: 
            if 'pct_chg' in quote: pchg = float(quote['pct_chg'])
            else: pchg = (close - pre_close) / pre_close * 100 if pre_close > 0 else 0.0
        except: pchg = 0.0
        
        try: turnover = float(quote.get('turnover_rate', quote.get('turnover', 0)))
        except: turnover = 0.0
        
        try: amount = float(quote.get('amount', 0)) / 100000000
        except: amount = 0.0
            
        lines.append(f"## 📊 {code[:6]} {name} 全息多维诊断 (V6)")
        lines.append("")
        
        # 1. Dashboard
        lines.append("### 一、核心概况与行情")
        lines.append("| 行情指标 | 数值 | 基本面信息 | 内容 |")
        lines.append("|---|---|---|---|")
        
        trend_info = tech_advice.get('trend', {}) or {}
        trend_state = trend_info.get('status') or trend_info.get('state') or '未知'
        index_rs_str = tech_advice.get('index_rs', '数据缺失')
        lines.append(f"| **最新价** | ¥{close:.2f} ({pchg:+.2f}%) | **行业板块** | {industry} |")
        lines.append(f"| **换手率** | {turnover:.2f}% | **主力动向** | {money_flow_str} |")
        lines.append(f"| **成交额** | {amount:.2f}亿 | **大盘对标** | {index_rs_str} |")
        lines.append(f"| **技术状态**| `{trend_state}` | **上市板块** | {stock_basic.get('market', '未知')} |")
        lines.append("")

        # Data quality overview (best-effort)
        dq = tech_advice.get('data_quality', {}) or {}
        if dq:
            from collections import Counter

            # Prefer showing only anomalies to keep the report readable.
            def _is_dq_issue(src: str, fallback_used: bool, note: str) -> bool:
                if fallback_used:
                    return True
                s = (src or '').lower()
                n = (note or '').lower()
                if 'fallback' in s or s not in {'api', 'cache'}:
                    return True
                keywords = ['incomplete', 'zero', 'missing', 'fail', 'error', 'limit', '异常', '缺失']
                return any(k in n for k in keywords)

            counts = Counter()
            issues = []
            for k, v in dq.items():
                if not isinstance(v, dict):
                    continue
                src = str(v.get('source', 'unknown') or 'unknown')
                note = str(v.get('note', '') or '').strip()
                fb = bool(v.get('fallback_used', False))
                counts[src] += 1
                if _is_dq_issue(src, fb, note):
                    if fb and 'fallback' not in src.lower():
                        src = f"{src}/fallback"
                    issues.append((k, src, note))

            lines.append("### 数据质量概览")
            api_cnt = counts.get('api', 0)
            cache_cnt = counts.get('cache', 0)

            if not issues:
                lines.append(f"- ✅ 未检测到降级/异常（api: {api_cnt}, cache: {cache_cnt}）")
                lines.append("")
            else:
                lines.append(f"- ⚠️ 检测到 {len(issues)} 项降级/异常（api: {api_cnt}, cache: {cache_cnt}）")
                lines.append("| 模块 | 来源 | 备注 |")
                lines.append("|---|---|---|")
                for k, src, note in issues:
                    lines.append(f"| {k} | {src} | {note or '-'} |")
                lines.append("")

        # Money flow details (best-effort)
        mf = tech_advice.get('money_flow', {}) or {}
        if mf:
            try:
                days = int(mf.get('days', 0) or 0)
                net_in = float(mf.get('net_inflow', 0) or 0)
                elg = float(mf.get('elg_net', 0) or 0)
                inflow_days = int(mf.get('inflow_days', 0) or 0)
                if days > 0:
                    lines.append("### 主力资金结构 (增强)")
                    net_str = f"{net_in/10000:+.2f}亿" if net_in else "0"
                    elg_str = f"{elg/10000:+.2f}亿" if elg else "0"
                    lines.append(f"- **近{days}日主力净流**: {net_str} | **净流入天数**: {inflow_days}/{days} | **超大单净额**: {elg_str}")
                    lines.append("")
            except Exception:
                pass

        vc = tech_advice.get('volume_context', {}) or {}
        if vc:
            def _fmt_float(value, digits=2, suffix=""):
                try:
                    if value is None:
                        return "-"
                    return f"{float(value):.{digits}f}{suffix}"
                except Exception:
                    return "-"

            try:
                amount_yi = float(vc.get('amount_yi', 0) or 0)
                turnover_v = vc.get('turnover')
                vol_ratio = vc.get('volume_ratio')
                vol_vs_5d = vc.get('vol_vs_5d')
                vol_vs_20d = vc.get('vol_vs_20d')

                volume_signal = "中性"
                volume_note = "当前量能需要结合关键价位是否站稳判断。"
                if vol_ratio is not None:
                    vr = float(vol_ratio or 0)
                    if vr >= 2:
                        volume_signal = "放量"
                        volume_note = "量比明显放大，若价格同步上攻并站稳，资金承接较好。"
                    elif vr < 0.8:
                        volume_signal = "缩量"
                        volume_note = "量能偏弱，若上攻关键位无量，容易冲高回落。"
                elif vol_vs_5d is not None:
                    v5 = float(vol_vs_5d or 0)
                    if v5 >= 1.5:
                        volume_signal = "相对放量"
                        volume_note = "当前成交量高于近5日均量，观察是否为主动买入推动。"
                    elif v5 < 0.8:
                        volume_signal = "相对缩量"
                        volume_note = "当前成交量低于近5日均量，突破可信度需要打折。"

                lines.append("### 量能与成交活跃度")
                lines.append(
                    f"- **成交额**: {_fmt_float(amount_yi, 2, '亿')} | "
                    f"**换手率**: {_fmt_float(turnover_v, 2, '%')} | "
                    f"**量比**: {_fmt_float(vol_ratio, 2)}"
                )
                lines.append(
                    f"- **相对均量**: 今日/近5日 {_fmt_float(vol_vs_5d, 2)} 倍 | "
                    f"今日/近20日 {_fmt_float(vol_vs_20d, 2)} 倍"
                )
                lines.append(f"- **量能结论**: {volume_signal}。{volume_note}")
                lines.append("- **实战口径**: 上攻关键价位要放量且站住；下跌若放量，要优先按风险处理。")
                lines.append("")
            except Exception:
                pass

        # 2. Position Diagnosis if buy_price is provided
        sl = tech_advice.get('stop_loss', 0)
        sup = tech_advice.get('support', 0)
        res = tech_advice.get('resistance', 0)
        
        next_idx = 2
        cn_numbers = {2:"二", 3:"三", 4:"四", 5:"五", 6:"六", 7:"七", 8:"八", 9:"九", 10:"十"}

        def _sec(idx: int) -> str:
            return cn_numbers.get(idx, str(idx))

        if buy_price and buy_price > 0:
            lines.append(f"### {_sec(next_idx)}、持仓风控与实战考量")
            next_idx += 1
            
            risk_metrics = tech_advice.get('risk_metrics', {})
            profit_loss = risk_metrics.get('current_profit', (close - buy_price) / buy_price * 100)
            diff_to_stop = (close - sl) / close * 100
            profit_str = f"**{profit_loss:+.2f}%**"
            
            hold_vol = int(position_context.get('hold_vol') or 0)
            pnl_amount = position_context.get('float_pnl_amount')
            break_even_need = position_context.get('break_even_need_pct')
            source_price = position_context.get('source_price')
            source_slippage = position_context.get('source_slippage_pct')
            selection = position_context.get('selection') or {}

            base_line = f"- **建仓成本**: ¥{buy_price:.2f} | **当前浮盈**: {profit_str}"
            if hold_vol > 0:
                base_line += f" | **持仓**: {hold_vol}股"
            if pnl_amount is not None:
                try:
                    base_line += f" | **浮动盈亏**: ¥{float(pnl_amount):+.2f}"
                except Exception:
                    pass
            lines.append(base_line)
            if break_even_need is not None:
                try:
                    lines.append(f"- **回本要求**: 从现价回到成本 ¥{buy_price:.2f} 还需要约 {float(break_even_need):+.2f}%")
                except Exception:
                    pass
            if source_price:
                src_line = f"- **系统信号溯源**: {selection.get('date','-')} {selection.get('strategy','-')} 入库价 ¥{float(source_price):.2f}"
                if source_slippage is not None:
                    src_line += f" | 你的买入价相对入库价滑点 {float(source_slippage):+.2f}%"
                lines.append(src_line)
                if source_slippage is not None and float(source_slippage) > 1.0:
                    lines.append("- **滑点提醒**: 真实买入价明显高于系统参考价，短线安全垫已被压缩，回本线附近不宜贪。")
            if selection:
                obs = selection.get('observe_status') or selection.get('zt_result') or '-'
                reason = selection.get('observe_reason') or '-'
                lines.append(f"- **观察状态**: {display_status(obs)} | 当前原因: {humanize_text(reason)}")
            if 'max_drawdown' in risk_metrics:
                lines.append(f"- **期间最大回撤**: {risk_metrics['max_drawdown']:.2f}% | **年化波动率**: {risk_metrics['volatility']:.2f}%")
            
            if profit_loss < 0:
                if close < sl:
                    lines.append("- 🚨 **警报**: 股价已**跌破量化止损位**，**建议立即无条件离场止损**，保住本金！")
                elif diff_to_stop < 2:
                    lines.append(f"- ⚠️ **警报**: 正在逼近止损位 ¥{sl:.2f} (仅差 {diff_to_stop:.1f}%)，一旦跌破必须出局。")
                else:
                    lines.append(f"- 💡 **建议**: 当前处于浮亏状态，但仍在止损线之上 (距离止损 {diff_to_stop:.1f}%)，可继续耐心持有。")
            else:
                if close > res:
                    lines.append("- ✨ **建议**: 股价已突破近期阻力位，动能强劲，可继续向上看高一线，止盈位可上移。")
                else:
                    dist_to_res = (res - close) / close * 100
                    lines.append(f"- 💡 **建议**: 处于盈利状态，距离上方阻力位 ¥{res:.2f} 还有约 {dist_to_res:.1f}% 空间，持股待涨。")
            # Risk plus (best-effort)
            rp = tech_advice.get('risk_plus', {}) or {}
            if rp:
                try:
                    if 'sharpe' in rp and rp.get('sharpe') is not None:
                        lines.append(f"- **夏普比率(近100日)**: {rp.get('sharpe'):.2f}")
                    if 'max_daily_drop_pct' in rp:
                        lines.append(f"- **单日最大跌幅(近100日)**: {rp.get('max_daily_drop_pct'):.2f}% | **单日最大涨幅**: {rp.get('max_daily_rise_pct'):.2f}%")
                    if 'gap_today_pct' in rp:
                        lines.append(f"- **今日跳空(Gap)**: {rp.get('gap_today_pct'):+.2f}%")
                    if 'gap_60d_max_abs_pct' in rp:
                        lines.append(f"- **近60日跳空幅度**: max|gap|={rp.get('gap_60d_max_abs_pct'):.2f}% | avg|gap|={rp.get('gap_60d_avg_abs_pct'):.2f}%")

                    atr_data = rp.get('atr_data') or {}
                    if isinstance(atr_data, dict) and atr_data.get('atr_percent') is not None:
                        lines.append(f"- **ATR波动档位**: {atr_data.get('volatility', 'unknown')} | ATR%={atr_data.get('atr_percent', 0):.2f}%")

                    ps = rp.get('position_suggest') or {}
                    if isinstance(ps, dict) and ps.get('quantity', 0) and ps.get('amount', 0):
                        lines.append(f"- **风险仓位建议(2%法则)**: 建议股数 {ps.get('quantity')} 股 | 约 ¥{ps.get('amount'):.0f} | 风险额 ¥{ps.get('risk_amount'):.0f} | 止损空间 {ps.get('stop_loss_pct')}% (总资产¥{ps.get('total_asset'):.0f})")
                except Exception:
                    pass

            lines.append("")

        # 3. V3 Enhanced Fundamentals & Concepts
        lines.append(f"### {_sec(next_idx)}、价值底与资金热度 (V3专版)")
        next_idx += 1
        
        # 财务排雷
        if fina:
            pe_ttm = fina.get('pe_ttm', 0)
            pb = fina.get('pb', 0)
            roe = fina.get('roe', 0)
            debt_ratio = fina.get('debt_to_assets', 0)
            
            pe_str = f"{pe_ttm:.1f}" if pd.notna(pe_ttm) else "未知"
            pb_str = f"{pb:.2f}" if pd.notna(pb) else "未知"
            roe_str = f"{roe:.2f}%" if pd.notna(roe) else "未知"
            debt_str = f"{debt_ratio:.2f}%" if pd.notna(debt_ratio) else "未知"
            
            fina_alert = ""
            if pd.notna(roe) and roe < 0: fina_alert += "⚠️ 当期ROE为负，业绩亏损。 "
            if pd.notna(debt_ratio) and debt_ratio > 80: fina_alert += "🚨 资产负债率超80%，债务风险极高！ "
            if pd.notna(pe_ttm) and pe_ttm > 100: fina_alert += "⚠️ 动态市盈率达百倍以上，估值偏高。 "
            if not fina_alert: fina_alert = "✅ 基本面暂无明显财报恶化信号。"
            
            lines.append(f"> **财务核心指标**: PE(TTM): `{pe_str}` | PB: `{pb_str}` | ROE: `{roe_str}` | 负债率: `{debt_str}`")
            lines.append(f"> **财务风控**: {fina_alert}")
            lines.append("")
            
        # 题材热点
        if concepts:
            # Show up to 8 Core Concepts to avoid massive walls of text
            show_concepts = [c for c in concepts if c not in ['融资融券', '深股通', '沪股通', '富时罗素概念股', '标普道琼斯A股', '中证500成份股']]
            concept_str = "、".join(show_concepts[:8])
            if len(show_concepts) > 8: concept_str += " 等..."
            lines.append(f"- **核心题材标签**: **{concept_str}**")
            
        # 机构调研
        if surv and surv.get('survey_times', 0) > 0:
            lines.append(f"- **机构动向**: 近30天被调研 **{surv['survey_times']}** 次 (共约 {surv['total_institutions']} 家机构参与)。🔥 *(机构密集调研是潜在利好信号)*")
        else:
            lines.append(f"- **机构动向**: 近30天未见明显公开机构批量调研记录。")
            
        # 融资融券
        if margin and margin.get('rzmre', 0) > 0:
            rzye_yi = margin['rzye'] / 100000000
            intensity = margin.get('rz_intensity', 0)
            intensity_str = f"🔥 融资买入占比达 **{intensity:.2f}%**" if intensity > 10 else f"占比 {intensity:.2f}%"
            lines.append(f"- **杠杆资金(游资)**: 最新融资余额 {rzye_yi:.2f} 亿 | 当日强买: {intensity_str}")
            
        lines.append("")

        # 4. Multi-Period & Resonance Scan
        lines.append(f"### {_sec(next_idx)}、多指标多周期共振扫描")
        next_idx += 1

        multi = tech_advice.get('multi_period', {})
        if multi:
            lines.append("| 周期 | 趋势 | 均线状态 |")
            lines.append("|---|---|---|")
            d_m = multi.get('daily', {})
            w_m = multi.get('weekly', {})
            mon_m = multi.get('monthly', {})
            lines.append(f"| **日线 (短期)** | {d_m.get('trend', '-')} | {d_m.get('status', '-')} |")
            lines.append(f"| **周线 (中期)** | {w_m.get('trend', '-')} | {w_m.get('status', '-')} |")
            lines.append(f"| **月线 (长期)** | {mon_m.get('trend', '-')} | {mon_m.get('status', '-')} |")
            lines.append("")

        advanced = tech_advice.get('advanced', {})
        signals = advanced.get('signals', [])
        resonance = advanced.get('resonance', 0)
        max_res = advanced.get('max_resonance', 4)

        lines.append(f"- **指标共振度**: **{resonance}/{max_res}** (分值越高多头/空头信号越强)")
        if signals:
            for sig in signals:
                lines.append(f"  - {sig}")

        # Trend strength (ADX)
        ts = tech_advice.get('trend_strength', {}) or {}
        if ts:
            try:
                adx = ts.get('adx')
                trend = ts.get('trend', '-')
                can_buy = ts.get('can_buy')
                reason = ts.get('reason', '')
                extra = f" | {'✅可顺势' if can_buy else '⚠️谨慎'}" if can_buy is not None else ""
                if adx is not None:
                    lines.append(f"- **趋势强度(ADX)**: {adx:.1f} | {trend}{extra} {reason}")
            except Exception:
                pass

        lines.append(f"- **波动率 (ATR)**: 日均波幅约 ¥{tech_advice.get('trend', {}).get('atr', 0):.2f}")
        lines.append(f"- **关键防守位**: 强支撑 ¥{sup:.2f} / **量化止损点 ¥{sl:.2f}**")
        lines.append(f"- **上行阻力位**: ¥{res:.2f}")
        lines.append("")

        # V6 extended contexts (industry/northbound/LHB/holders)
        industry_ctx = v3_data.get('industry_context') or {}
        if industry_ctx:
            lines.append(f"### {_sec(next_idx)}、🏭 行业/板块环境")
            next_idx += 1
            try:
                ind = industry_ctx.get('industry', '-')
                daily = industry_ctx.get('daily') or {}
                drc = industry_ctx.get('daily_rank_change')
                drm = industry_ctx.get('daily_rank_moneyflow')
                lines.append(f"- **所属行业**: {ind}")
                if daily:
                    lines.append(f"- **行业当日均涨幅**: {daily.get('avg_change', 0):.2f}% | **当日净流**: {float(daily.get('net_money_flow', 0) or 0)/10000:.2f}亿 | 涨幅排名: {drc or '-'} | 资金排名: {drm or '-'}")
                agg = industry_ctx.get('agg') or {}
                if agg:
                    lines.append(f"- **行业近{industry_ctx.get('agg_days', 10)}日资金净流**: {float(agg.get('net_inflow', 0) or 0)/10000:.2f}亿 | 排名: {industry_ctx.get('agg_rank') or '-'}")
                    top_stocks = industry_ctx.get('top_stocks') or []
                    if top_stocks:
                        top_str = "、".join([f"{(s.get('code') or '')[:6]}({float(s.get('net_inflow',0) or 0)/10000:.2f}亿)" for s in top_stocks[:5]])
                        lines.append(f"- **行业资金Top**: {top_str}")
                    if industry_ctx.get('is_top_stock'):
                        lines.append("- ✅ **行业资金龙头信号**: 本股在行业资金Top名单内")
                lines.append("")
            except Exception:
                lines.append("- 行业环境数据缺失")
                lines.append("")

        north = v3_data.get('northbound') or {}
        if north:
            lines.append(f"### {_sec(next_idx)}、🧭 北向资金画像")
            next_idx += 1
            try:
                elig = north.get('eligibility') or {}
                is_hsgt = elig.get('is_hsgt') if isinstance(elig, dict) else None
                types = elig.get('type') if isinstance(elig, dict) else []
                lines.append(f"- **港股通标的**: {'是' if is_hsgt else '否'} {('(' + ','.join(types) + ')') if types else ''}")
                if north.get('in_top10'):
                    hit = north.get('top10') or {}
                    lines.append(f"- **北向Top10**({north.get('trade_date')}): rank={hit.get('rank')} | net={float(hit.get('net_amount',0) or 0)/10000:.2f}亿 | amount={float(hit.get('amount',0) or 0)/10000:.2f}亿")
                else:
                    lines.append(f"- **北向Top10**({north.get('trade_date')}): 未进入榜单")
                lines.append("")
            except Exception:
                lines.append("")

        lhb = v3_data.get('lhb_recent') or []
        if lhb is not None:
            lines.append(f"### {_sec(next_idx)}、🐉 龙虎榜/异动风险")
            next_idx += 1
            try:
                if isinstance(lhb, list) and lhb:
                    lines.append(f"- **近5日上榜次数**: {len(lhb)}")
                    for r in lhb[:5]:
                        td = r.get('trade_date', '-')
                        reason = r.get('reason', '-')
                        net = float(r.get('net_amount', 0) or 0)
                        lines.append(f"  - {td}: {reason} | 净额 {net/10000:.2f}亿")
                    lines.append("- ⚠️ 上榜通常意味着波动与换手放大：执行上建议更严格的止损/分批。")
                else:
                    lines.append("- 近5日未见公开龙虎榜上榜记录。")
                lines.append("")
            except Exception:
                lines.append("")

        holders = v3_data.get('holders') or {}
        if holders:
            lines.append(f"### {_sec(next_idx)}、👥 筹码与股东结构")
            next_idx += 1
            try:
                hn = holders.get('holder_number') or {}
                if isinstance(hn, dict) and hn.get('current'):
                    lines.append(f"- **股东人数**: {hn.get('current')} (上期 {hn.get('previous')}) | 变化 {hn.get('change_rate', 0):+.2f}%")
                sf = holders.get('state_fund') or {}
                if isinstance(sf, dict) and sf.get('has_state_fund'):
                    fs = sf.get('funds') or []
                    sample = "、".join(fs[:5])
                    lines.append(f"- ✅ **国家队/社保等出现**: {sample}{' ...' if len(fs) > 5 else ''}")
                else:
                    lines.append("- 国家队/社保等前十大股东信号: 未检测到明确命中(关键词匹配)。")
                lines.append("")
            except Exception:
                lines.append("")

        if buy_price and buy_price > 0:
            try:
                source_price = position_context.get('source_price')
                break_even = float(buy_price)
                repair_line = float(source_price or 0) if source_price else None
                current = close
                hard_stop = float(sl or 0)
                support_line = float(sup or 0)
                resistance = float(res or 0)
                pivot_line = None
                chart_meta = position_context.get('chart_meta') or {}
                if isinstance(chart_meta, dict):
                    pivot_line = chart_meta.get('pivot')

                lines.append(f"### {_sec(next_idx)}、持仓操作决策树")
                next_idx += 1
                lines.append(f"- **当前定位**: 现价 ¥{current:.2f}，成本 ¥{break_even:.2f}，回本线就是第一压力位。")
                if repair_line:
                    lines.append(f"- **信号修复线**: 先看能否重新站上系统入库价 ¥{repair_line:.2f}。站不上，说明原午盘资金信号仍未修复。")
                lines.append(f"- **回本处理线**: 接近/站上 ¥{break_even:.2f} 时，若分时放量但不能继续站稳，优先减亏或落袋，不按入库价绩效盲目乐观。")
                if resistance > 0:
                    lines.append(f"- **盈利确认线**: 放量突破并站稳 ¥{resistance:.2f} 后，才看作进入盈利弹性区。")
                if hard_stop > 0:
                    lines.append(f"- **短线风控线**: 跌破量化止损 ¥{hard_stop:.2f} 且放量/收不回，优先按短线失败处理。")
                if support_line > 0:
                    lines.append(f"- **结构支撑线**: ¥{support_line:.2f} 是日线结构支撑，若连这里也跌破，趋势修复逻辑基本失效。")
                lines.append("- **量能确认**: 上攻修复线/回本线必须放量并站稳；下跌若放量，按风险优先。")
                lines.append("")
            except Exception:
                pass

        # 5. Actionable Advice
        lines.append(f"### {_sec(next_idx)}、🤖 交易系统策略执行建议")
        lines.append(f"- **当前操作**: **{tech_advice.get('trend', {}).get('action', '观望')}**")
        lines.append(f"- **动能强度**: {tech_advice.get('trend', {}).get('desc', '-')}")

        # Risk sizing hint
        rp2 = tech_advice.get('risk_plus', {}) or {}
        ps2 = rp2.get('position_suggest') if isinstance(rp2, dict) else None
        if isinstance(ps2, dict) and ps2.get('quantity', 0) and ps2.get('amount', 0):
            lines.append(f"- **风险约束仓位(2%法则)**: 建议 {ps2.get('quantity')}股 (~¥{ps2.get('amount'):.0f}) | 止损空间 {ps2.get('stop_loss_pct')}% | 波动档位 {ps2.get('volatility')}")

        lines.append("")
        lines.append("*注: 诊断数据基于纯量化模型结合 Tushare Pro 数据自动生成，本投资辅助工具不构成绝对投资建议。*")
        
        return "\n".join(lines)

    # =========================================
    # INDIVIDUAL STOCK ANALYSIS (V4 Graphic T+0)
    # =========================================
    def generate_stock_report_v4(self, stock_basic, quote, tech_advice, money_flow_str="数据缺失", buy_price=None, v3_data=None, chart_paths=None, t0_advice=None, position_context=None):
        """
        [V4] Generate holographic individual stock analysis report with T+0 charts
        """
        # Inherit all the text from V3
        v3_report = self.generate_stock_report_v3(stock_basic, quote, tech_advice, money_flow_str, buy_price, v3_data, position_context=position_context)
        
        lines = [v3_report]
        
        if chart_paths or t0_advice:
            lines.append("")
            lines.append("---")
            lines.append("### 📈 T+0 分时与日线全景导引图 (V4专版)")
            lines.append("")
            
            # [Phase 20] Display T+0 Quantitative Score & Historical Stats
            t0_score_data = tech_advice.get('t0_score_data', {})
            t0_stats = tech_advice.get('t0_stats', {})
            score_val = t0_score_data.get('score', 0)
            score_desc = t0_score_data.get('desc', '-')
            
            if score_val > 0:
                lines.append(f"**【量化做T潜力评估】**: **{score_val}分** | 综合评级: **{score_desc}**")
                
                # Show breakdown details
                details = t0_score_data.get('details', {})
                if details:
                    lines.append("> 📊 **因子得分拆解**:")
                    if 'ma_dev' in details: lines.append(f"> - 均线偏离度({details['ma_dev']})")
                    if 'support' in details: lines.append(f"> - 支撑阻力区({details['support']})")
                    if 'volume' in details: lines.append(f"> - 量价配合({details['volume']})")
                    if 'trend' in details: lines.append(f"> - 大势环境({details['trend']})")
                lines.append("")
            
            if t0_stats and t0_stats.get('count', 0) > 0:
                lines.append(f"**【历史做T纪要(本股)】**: 共计操作 **{t0_stats['count']}** 次 | 胜率: **{t0_stats['win_rate']:.1f}%** | 总盈亏: **{t0_stats['total_pnl']:.2f}元** (均笔 **{t0_stats['avg_pnl']:.2f}元**)")
                lines.append("")
            
            if chart_paths:
                daily = chart_paths.get('daily')
                intraday = chart_paths.get('intraday')
                meta = chart_paths.get('_meta') if isinstance(chart_paths, dict) else {}
                if isinstance(meta, dict) and meta:
                    points = int(meta.get('intraday_points') or 0)
                    quality = meta.get('intraday_quality') or 'unknown'
                    last_time = meta.get('intraday_last_time') or '-'
                    if quality != 'ok':
                        lines.append(f"> **分时数据提示**: 当前仅 {points} 个分钟点，最后时间 {last_time}，分时成交量/做T判断只能弱参考。")
                        lines.append("")
                if daily:
                    lines.append("#### 1. 日线图 (含持仓成本线)")
                    # Embed local markdown image
                    lines.append(f"![日线走势图](file:///{daily.replace(chr(92), '/')})")
                    lines.append("")
                if intraday:
                    lines.append("#### 2. 分时图 (含做T阻力支撑与VWAP均线)")
                    lines.append(f"![今日分时图](file:///{intraday.replace(chr(92), '/')})")
                    lines.append("")
            
            if t0_advice:
                lines.append("#### 💡 日内网格做T指导策略")
                lines.append(t0_advice)
                lines.append("")
            
            lines.append("*图表生成完毕，旧图像自动执行 7 天生命周期清理。*")
            
        return "\n".join(lines)
    
    # =========================================
    # STRATEGY EVOLUTION NOTIFICATION
    # =========================================
    def format_notify_update(self, message, config):
        """Format strategy evolution notification"""
        lines = []
        date_str = datetime.now().strftime('%Y-%m-%d %H:%M')
        
        lines.append("=" * 60)
        lines.append(f"🧬 策略进化通知 ({date_str})")
        lines.append("=" * 60)
        lines.append("")
        lines.append("【变更内容】")
        lines.append(message)
        lines.append("")
        
        if config:
            lines.append("⚙️【最新策略参数】")
            import json
            lines.append(json.dumps(config, indent=2, ensure_ascii=False))
            lines.append("")
            
            # Highlight key params
            auc = config.get('auction', {})
            aft = config.get('afternoon', {})
            macd = config.get('macd', {})
            
            lines.append("📋【参数一览】")
            if auc:
                lines.append(f"  竞价: 高开 {auc.get('min_open_change')}-{auc.get('max_open_change')}% / 换手>{auc.get('min_turnover')}%")
            if aft:
                lines.append(f"  午盘: 涨幅 {aft.get('min_change')}-{aft.get('max_change')}% / 换手 {aft.get('min_turnover')}-{aft.get('max_turnover')}% / 趋势因子 {aft.get('trend_factor')}")
            if macd:
                lines.append(f"  MACD: Fast={macd.get('fast')} / Slow={macd.get('slow')} / Signal={macd.get('signal')}")
            lines.append("")
        
        lines.append("📦【配置入库】")
        lines.append("  ✅ strategy_config.json: 已更新策略配置文件")
        lines.append("  🔄 下次执行时自动加载新参数")
        lines.append("")
        lines.append("-" * 60)
        lines.append("注: 策略参数调整不会影响已有持仓")
        
        return "\n".join(lines)

    def send_email(self, subject, content):
        """Send report via email"""
        _log_email_snapshot(subject, content, status="READY")
        if not Config.EMAIL_ENABLED:
            logger.info("Email delivery disabled, skipping send.")
            _log_email_result(subject, status="SKIPPED", extra="EMAIL_ENABLED is false")
            return False
        if not Config.EMAIL_USER or not Config.EMAIL_PWD or not Config.EMAIL_TO:
            logger.warning("Email configuration incomplete, skipping send.")
            _log_email_result(subject, status="SKIPPED", extra="email config incomplete")
            return False
            
        try:
            # Convert text content to HTML format and extract images
            images_to_embed = {}
            html_content = _text_to_html(content, images_to_embed)
            
            # Send Multipart Email
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText
            from email.mime.image import MIMEImage
            
            msg = MIMEMultipart('related')
            msg['Subject'] = Header(subject, 'utf-8')
            msg['From'] = Config.EMAIL_USER
            msg['To'] = ', '.join(Config.EMAIL_TO)
            
            # Provide alternative plain text / html for the body
            msg_alternative = MIMEMultipart('alternative')
            msg.attach(msg_alternative)
            
            part1 = MIMEText(content, 'plain', 'utf-8')
            part2 = MIMEText(html_content, 'html', 'utf-8')
            
            msg_alternative.attach(part1)
            msg_alternative.attach(part2)
            
            # Attach inline images
            for cid, path in images_to_embed.items():
                try:
                    import os
                    if os.path.exists(path):
                        with open(path, 'rb') as f:
                            img_data = f.read()
                        image = MIMEImage(img_data)
                        image.add_header('Content-ID', f'<{cid}>')
                        image.add_header('Content-Disposition', 'inline', filename=os.path.basename(path))
                        msg.attach(image)
                except Exception as e:
                    logger.error(f"Failed to attach image {path}: {e}")
            
            server = smtplib.SMTP_SSL(Config.SMTP_SERVER, Config.SMTP_PORT)
            server.login(Config.EMAIL_USER, Config.EMAIL_PWD)
            server.sendmail(Config.EMAIL_USER, list(Config.EMAIL_TO), msg.as_string())
            server.quit()
            logger.info(f"Email sent to {', '.join(Config.EMAIL_TO)} with {len(images_to_embed)} images")
            _log_email_result(subject, status="SENT", extra=f"to={', '.join(Config.EMAIL_TO)}; inline_images={len(images_to_embed)}")
            return True
        except Exception as e:
            logger.error(f"Failed to send email: {e}")
            _log_email_result(subject, status="FAILED", extra=str(e))
            return False
