package com.insurance.rag.filter;

import java.util.List;

/**
 * 过滤结果。
 *
 * <p>设计要点：掩码与拦截是两件事，不能混在一个 boolean 里。
 * 掩码后仍然允许请求继续（只是落库/转发的是掩码值），
 * 拦截则直接终止并记审计。
 */
public record FilterResult(
        String text,
        boolean blocked,
        List<String> hits,
        List<String> maskedSpans) {

    public boolean hasHit() {
        return !hits.isEmpty();
    }

    public boolean hasMask() {
        return !maskedSpans.isEmpty();
    }

    static FilterResult pass(String text) {
        return new FilterResult(text, false, List.of(), List.of());
    }
}