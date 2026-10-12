package com.insurance.rag.dify;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.List;

/**
 * Dify SSE 事件与收尾元数据的最小映射。
 *
 * <p>只用 JsonNode 兜不住字段缺失，这里对每个字段都给出默认值——
 * Dify 在不同事件里返回的字段集并不一致（例如 message 事件没有 metadata，
 * message_end 没有 answer）。
 */
public final class DifyEvents {

    private DifyEvents() {
    }

    /** SSE 事件载荷。 */
    @JsonIgnoreProperties(ignoreUnknown = true)
    public static class Event {
        public String event;
        public String task_id;
        public String message_id;
        public String conversation_id;
        public String answer;
        public String id;
        public Metadata metadata;
        public String code;
        public String status;
        public String message;
    }

    /** 收尾元数据：用量 + 引用来源。 */
    @JsonIgnoreProperties(ignoreUnknown = true)
    public static class Metadata {
        @JsonProperty("retriever_resources")
        public List<RetrieverResource> retrieverResources;
        public Usage usage;

        /** Dify 可能返回 null，这里统一成空列表，避免调用方到处判空。 */
        public List<RetrieverResource> resources() {
            return retrieverResources == null ? List.of() : retrieverResources;
        }
    }

    /**
     * 单条引用。Dify 的字段名以 dataset_/segment_ 前缀区分数据集与文档内的分段。
     *
     * <p>注意：{@code score} 在 Rerank 开启时是 rerank 分数，未开启时是向量相似度——
     * 两者量纲不同，记录时要连同是否启用 rerank 一起留痕，否则无法复现调参结论。
     */
    @JsonIgnoreProperties(ignoreUnknown = true)
    public static class RetrieverResource {
        @JsonProperty("dataset_id")
        public String datasetId;
        @JsonProperty("dataset_name")
        public String datasetName;
        @JsonProperty("document_id")
        public String documentId;
        @JsonProperty("document_name")
        public String documentName;
        @JsonProperty("segment_id")
        public String segmentId;
        @JsonProperty("segment_position")
        public Integer segmentPosition;
        @JsonProperty("dataset_position")
        public Integer datasetPosition;
        public Double score;
        public String content;
        public String title;
        public String icon;
        public String type;
    }

    @JsonIgnoreProperties(ignoreUnknown = true)
    public static class Usage {
        @JsonProperty("total_tokens")
        public Integer totalTokens;
        @JsonProperty("prompt_tokens")
        public Integer promptTokens;
        @JsonProperty("completion_tokens")
        public Integer completionTokens;
    }

    /** 问答结果汇总。 */
    public record ChatResult(String answer,
                             String conversationId,
                             List<RetrieverResource> resources,
                             Usage usage,
                             boolean refused,
                             long elapsedMs) {
    }
}