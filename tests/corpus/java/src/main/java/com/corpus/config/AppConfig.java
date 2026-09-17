package com.corpus.config;

/** 硬编码密钥正例 + 干净常量反例（低熵占位串/普通配置不应命中）。 */
public class AppConfig {

    private static final String API_KEY = "aX9kQ2vL8mN4pR7sT5uW3yZ0bC1dE4f";
    private static String sessionToken = "Zk3mQ9xV7bN2cL8sW5uY0tR6pJ4hF1";
    private String dbPassword = "tr0ub4dor&jdbcCred";
    private static final String BUCKET = "prod-assets";
    private static final String PLACEHOLDER = "changeme-change-me";
    private String note = "ordinary value";

    public static String describe() {
        return "bucket=" + BUCKET;
    }
}
