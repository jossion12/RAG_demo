package com.insurance.rag.filter;

import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.springframework.stereotype.Component;

/**
 * 敏感信息过滤（FR-08）。
 *
 * <p><b>过滤对象</b>（V1.0 未定义，此处按「本项目数据源为公开条款、全程不接触真实客户
 * PII」这一事实收敛为三类）：
 * <ol>
 *   <li><b>输入侧 PII</b>：身份证号 / 手机号 / 银行卡号 —— 掩码后继续，保证日志与落库不含明文；</li>
 *   <li><b>输入侧承诺语</b>：用户以「保证收益」类话术提问时，先行拦截 —— 因为这类问题即使
 *       知识库里没有，也极易诱导模型编造；</li>
 *   <li><b>输出侧承诺语</b>：模型输出出现「稳赚不赔」等金融承诺，拦截并记审计。</li>
 * </ol>
 *
 * <p>掩码保留首尾各若干位，既能让人眼核对，又不能复原完整证件号。
 */
@Component
public class SensitiveDataFilter {

    /**
     * 证件号模式的边界一律用 {@code (?&lt;!\d)} / {@code (?!\d)}，不用 {@code \b}。
     *
     * <p>原因（实测）：{@code \b} 依赖「单词字符 / 非单词字符」的划分，而 Java 正则的
     * {@code \w} 只有 {@code [a-zA-Z_0-9]}，中文与中文标点全部算「非单词字符」。
     * 在中文语境里这个划分不可靠——实测「我的身份证是110101199003078515」用
     * {@code \b(\d{17}[\dXx])\b} 匹配失败，去掉 \b 反而成功。
     *
     * <p>而「前后不是数字」正是这里的真实需求：既能完整吃掉 18 位身份证，
     * 又不会从更长的数字串里截取一段（那会造成漏判）。
     */
    private static final Pattern ID_CARD =
            Pattern.compile("(?<!\\d)(\\d{17}[\\dXx]|\\d{15})(?!\\d)");

    /** 手机号：1 开头第二位 3-9，共 11 位 */
    private static final Pattern PHONE =
            Pattern.compile("(?<!\\d)(1[3-9]\\d{9})(?!\\d)");

    /** 银行卡：16-19 位连续数字 */
    private static final Pattern BANK_CARD =
            Pattern.compile("(?<!\\d)(\\d{16,19})(?!\\d)");

    /** 金融承诺语：合规红线，出现即拦截 */
    private static final Pattern[] FORBIDDEN_PHRASES = {
            Pattern.compile("保证收益"),
            Pattern.compile("稳赚不赔"),
            Pattern.compile("保本保息"),
            Pattern.compile("零风险(?:投资|理财)?"),
            Pattern.compile("包赚"),
            Pattern.compile("年化收益(?:率)?\\s*[约达到]?\\s*\\d+\\s*%"),
            Pattern.compile("(?:一定能|必定|肯定)(?:拿到|获得|理赔成功)"),
    };

    private static final String[] FORBIDDEN_NAMES = {
            "保证收益", "稳赚不赔", "保本保息", "零风险", "包赚",
            "承诺年化收益率", "承诺理赔结果",
    };

    /**
     * 入参过滤：PII 掩码 + 承诺语拦截。
     *
     * @return 掩码/拦截后的结果；{@code blocked=true} 时调用方应终止请求并记审计
     */
    public FilterResult filterInput(String raw) {
        if (raw == null || raw.isBlank()) {
            return FilterResult.pass(raw);
        }
        List<String> hits = new ArrayList<>();
        List<String> masked = new ArrayList<>();

        String text = maskOne(raw, ID_CARD, "身份证号", masked);
        text = maskOne(text, PHONE, "手机号", masked);
        text = maskOne(text, BANK_CARD, "银行卡号", masked);

        for (int i = 0; i < FORBIDDEN_PHRASES.length; i++) {
            if (FORBIDDEN_PHRASES[i].matcher(text).find()) {
                hits.add(FORBIDDEN_NAMES[i]);
            }
        }
        return new FilterResult(text, !hits.isEmpty(), hits, masked);
    }

    /** 出参过滤：只做承诺语拦截，不做 PII 掩码（模型输出里的长数字可能是金额而非证件号）。 */
    public FilterResult filterOutput(String raw) {
        if (raw == null || raw.isBlank()) {
            return FilterResult.pass(raw);
        }
        List<String> hits = new ArrayList<>();
        for (int i = 0; i < FORBIDDEN_PHRASES.length; i++) {
            if (FORBIDDEN_PHRASES[i].matcher(raw).find()) {
                hits.add(FORBIDDEN_NAMES[i]);
            }
        }
        return new FilterResult(raw, !hits.isEmpty(), hits, List.of());
    }

    private String maskOne(String text, Pattern pattern, String label, List<String> sink) {
        Matcher m = pattern.matcher(text);
        StringBuilder sb = new StringBuilder();
        while (m.find()) {
            String value = m.group(1);
            m.appendReplacement(sb, Matcher.quoteReplacement(mask(value)));
            sink.add(label);
        }
        m.appendTail(sb);
        return sb.toString();
    }

    /** 保留首 3 尾 4（身份证/银行卡）或首 3 尾 4（手机号），中间以 * 代替。 */
    static String mask(String value) {
        int len = value.length();
        if (len <= 7) {
            return "*".repeat(len);
        }
        return value.substring(0, 3) + "*".repeat(len - 7) + value.substring(len - 4);
    }
}