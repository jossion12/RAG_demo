package com.insurance.rag.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * Dify 接入配置。
 *
 * <p>所有密钥只存在服务端（FR：密钥不得下发到浏览器）。Dify 的两类密钥互不通用：
 * <ul>
 *   <li>{@code appApiKey} —— 应用级，调用 Chatflow 问答；</li>
 *   <li>{@code datasetApiKeys} —— 知识库级，调用文档/知识库接口。</li>
 * </ul>
 */
@ConfigurationProperties(prefix = "dify")
public class DifyProperties {

    /** Dify 服务地址，例如 http://localhost（走 nginx 的 80 端口） */
    private String baseUrl = "http://localhost";

    /** 应用 API Key（app- 开头），用于问答 */
    private String appApiKey = "";

    /** 知识库 API Key，key = 产品标识（critical / medical），用于文档管理 */
    private java.util.Map<String, String> datasetApiKeys = new java.util.LinkedHashMap<>();

    /** 终端用户标识，本 Demo 固定单用户 */
    private String endUser = "demo-user";

    /** 单次问答超时（毫秒）。本地模型首 token 可能较慢，给足余量。 */
    private int readTimeoutMs = 180_000;

    public String getBaseUrl() {
        return baseUrl;
    }

    public void setBaseUrl(String baseUrl) {
        this.baseUrl = baseUrl;
    }

    public String getAppApiKey() {
        return appApiKey;
    }

    public void setAppApiKey(String appApiKey) {
        this.appApiKey = appApiKey;
    }

    public java.util.Map<String, String> getDatasetApiKeys() {
        return datasetApiKeys;
    }

    public void setDatasetApiKeys(java.util.Map<String, String> datasetApiKeys) {
        this.datasetApiKeys = datasetApiKeys;
    }

    public String getEndUser() {
        return endUser;
    }

    public void setEndUser(String endUser) {
        this.endUser = endUser;
    }

    public int getReadTimeoutMs() {
        return readTimeoutMs;
    }

    public void setReadTimeoutMs(int readTimeoutMs) {
        this.readTimeoutMs = readTimeoutMs;
    }
}