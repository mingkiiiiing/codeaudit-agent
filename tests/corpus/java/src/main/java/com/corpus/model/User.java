package com.corpus.model;

/** 领域模型干净反例：字段 + 构造器 + getter。 */
public class User {

    private final int id;
    private final String name;

    public User(int id, String name) {
        this.id = id;
        this.name = name;
    }

    public int id() {
        return id;
    }

    public String name() {
        return name;
    }

    public String display() {
        return name + "#" + id;
    }
}
