package com.insurance.rag.domain;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.time.Instant;

/**
 * 会话记录（FR-07）。
 *
 * <p>落库字段按「可审计」设计：除问答内容外，还保留检索分数、是否拒答、耗时。
 * 这些字段看起来冗余，但调参时能回答「这次为什么拒了」「当时分数是多少」，
 * 只存问答正文的话，评测集复现不了当时结论。
 */
@Entity
@Table(name = "conversation")
public class Conversation {

    @Id
    @Column(length = 64)
    private String id;

    /** Dify 侧会话 ID，便于回查 Dify 原文 */
    @Column(name = "dify_conversation_id", length = 64)
    private String difyConversationId;

    @Column(nullable = false, length = 2000)
    private String question;

    @Column(length = 8000)
    private String answer;

    /** 引用列表 JSON */
    @Column(columnDefinition = "TEXT")
    private String citations;

    /** 检索分数 JSON，用于复现调参结论 */
    @Column(columnDefinition = "TEXT")
    private String scores;

    /** 是否被拒答（无任何片段过阈值，或被路由层判为无关） */
    @Column(name = "is_refused")
    private boolean refused;

    @Column(name = "elapsed_ms")
    private Long elapsedMs;

    /** 入参过滤是否发生掩码，记录标签而非明文 */
    @Column(columnDefinition = "TEXT")
    private String maskedLabels;

    @Column(name = "created_at", nullable = false)
    private Instant createdAt;

    public String getId() {
        return id;
    }

    public void setId(String id) {
        this.id = id;
    }

    public String getDifyConversationId() {
        return difyConversationId;
    }

    public void setDifyConversationId(String difyConversationId) {
        this.difyConversationId = difyConversationId;
    }

    public String getQuestion() {
        return question;
    }

    public void setQuestion(String question) {
        this.question = question;
    }

    public String getAnswer() {
        return answer;
    }

    public void setAnswer(String answer) {
        this.answer = answer;
    }

    public String getCitations() {
        return citations;
    }

    public void setCitations(String citations) {
        this.citations = citations;
    }

    public String getScores() {
        return scores;
    }

    public void setScores(String scores) {
        this.scores = scores;
    }

    public boolean isRefused() {
        return refused;
    }

    public void setRefused(boolean refused) {
        this.refused = refused;
    }

    public Long getElapsedMs() {
        return elapsedMs;
    }

    public void setElapsedMs(Long elapsedMs) {
        this.elapsedMs = elapsedMs;
    }

    public String getMaskedLabels() {
        return maskedLabels;
    }

    public void setMaskedLabels(String maskedLabels) {
        this.maskedLabels = maskedLabels;
    }

    public Instant getCreatedAt() {
        return createdAt;
    }

    public void setCreatedAt(Instant createdAt) {
        this.createdAt = createdAt;
    }
}