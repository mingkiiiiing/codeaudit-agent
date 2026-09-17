package com.corpus.config;

/** 密钥正例第二形态：sk- 前缀 API key 与局部变量口令；含干净反例文案。 */
public class LegacyCredentials {

    public String bootstrap() {
        String apiKey = "sk-projdZ8sKq2mN7xVb3cLw9";
        String greeting = "welcome to the platform";
        connect(apiKey);
        return greeting;
    }

    private void connect(String cred) {
        System.out.println(cred.length());
    }
}
