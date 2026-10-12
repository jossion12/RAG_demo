package com.insurance.rag.citation;

import com.insurance.rag.dify.DifyEvents;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.springframework.stereotype.Component;

/**
 * 把 Dify 的 retriever_resources 组装成业务 Citation（FR-06）。
 *
 * <p>额外做两件 Dify 不会替你做的事：
 * <ol>
 *   <li><b>去重</b>：同一个 segment 可能被多条检索路径召回，不去重会让前端引用列表出现重复项；</li>
 *   <li><b>从正文里回捞条款号</b>：条款原文开头形如「2.3 等待期」，Dify 的 metadata 里没有条款号，
 *       但它是营销员真正要找的东西——「免责条款在哪」比「第 8 段」有用得多。</li>
 * </ol>
 */
@Component
public class CitationAssembler {

    /** 片段开头若形如「2.3 等待期」「2.3.1 身故保险金」则提取条款号 */
    private static final Pattern CLAUSE_NO = Pattern.compile("^\\s*(\\d{1,2}(?:\\.\\d{1,2}){0,2})\\s+(\\S[^\\n]{0,20})");

    private static final int SNIPPET_MAX = 300;

    public List<Citation> assemble(List<DifyEvents.RetrieverResource> resources) {
        if (resources == null || resources.isEmpty()) {
            return List.of();
        }
        Map<String, Citation> byKey = new LinkedHashMap<>();
        for (DifyEvents.RetrieverResource r : resources) {
            if (r == null) {
                continue;
            }
            String key = r.segmentId != null ? r.segmentId
                    : (r.documentId + "#" + r.segmentPosition);
            byKey.putIfAbsent(key, toCitation(r));
        }
        return new ArrayList<>(byKey.values());
    }

    private Citation toCitation(DifyEvents.RetrieverResource r) {
        return new Citation(
                r.datasetId,
                r.datasetName,
                r.documentId,
                r.documentName,
                r.segmentId,
                r.segmentPosition,
                extractPageNumber(r.content),
                r.score,
                clip(r.content));
    }

    /** 溯源粒度探测：正文里若带「第 N 页」则能给出页码，否则保持 null。 */
    static Integer extractPageNumber(String content) {
        if (content == null) {
            return null;
        }
        Matcher m = Pattern.compile("第\\s*(\\d{1,3})\\s*页").matcher(content);
        return m.find() ? Integer.valueOf(m.group(1)) : null;
    }

    /** 从片段开头提取条款号与标题，供前端展示「2.3 等待期」这种更有用的锚点。 */
    public static String clauseAnchor(String content) {
        if (content == null) {
            return null;
        }
        Matcher m = CLAUSE_NO.matcher(content);
        if (!m.find()) {
            return null;
        }
        return m.group(1) + " " + m.group(2).trim();
    }

    private String clip(String content) {
        if (content == null) {
            return null;
        }
        String flat = content.replaceAll("\\s+", " ").trim();
        return flat.length() <= SNIPPET_MAX ? flat : flat.substring(0, SNIPPET_MAX) + "…";
    }
}