package com.corpus.util;

/** 工具类干净反例：短方法、无安全/长函数问题。 */
public class MathHelper {

    public int add(int a, int b) {
        return a + b;
    }

    public String greet(String who) {
        return "hello, " + who;
    }
}
