package com.insurance.rag.citation;

import com.fasterxml.jackson.annotation.JsonInclude;

/**
 * 结构化引用（FR-06）。
 *
 * <p>{@code pageNumber} 设计为可空是有原因的：溯源粒度取决于 Dify 分段是否保留 PDF 页定位。
 * 实测确认前不能假定它一定存在——若降级为「文档 + 分段序号 + 原文片段」，
 * 这个字段就保持 null，而不是填一个假页码。溯源口径宁可粗一点，也不能给错。
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record Citation(
        String datasetId,
        String datasetName,
        String documentId,
        String documentName,
        String segmentId,
        Integer segmentPosition,
        Integer pageNumber,
        Double score,
        String snippet,
        String fullText) {

    /**
     * 片段**未截断**的原文，仅供机器判定与审计，前端展示请用 {@code snippet}。
     *
     * <p>为什么必须单独留一份：{@code snippet} 为展示截到 300 字
     * （{@code CitationAssembler.SNIPPET_MAX}），而条款片段中位 624 字、最长 1019 字，
     * <b>章节标题常落在 300 字之外</b>。评测的章节命中判定如果读 snippet，
     * 就会把「明明召回到了」判成 citation_miss / clause_mismatch——
     * 实测 34 条引用判定里 10 条受此影响。
     *
     * <p>展示层可以截断，判定层不行：截断是给人看的，判定要拿原件。
     * 两者共用一个字段，就等于强迫判据在残缺的副本上做判断。
     *
     * <p>走同一条 SSE 通道而不是另开审计接口，是为了让前端与评测**同源**：
     * 一旦拆成两个数据源，它们迟早会各自漂移——本项目已经因为
     * delta 载荷格式只同步了前端、漏了评测脚本，吃过一次静默错判。
     */
    public Citation {
        if (fullText == null) {
            fullText = snippet;
        }
    }

    /** 前端展示用的简短出处标签。 */
    public String label() {
        String base = documentName == null ? "未知文档" : documentName;
        if (pageNumber != null) {
            return base + " 第" + pageNumber + "页";
        }
        if (segmentPosition != null) {
            return base + " 第" + segmentPosition + "段";
        }
        return base;
    }
}