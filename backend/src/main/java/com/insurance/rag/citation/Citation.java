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
        String snippet) {

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