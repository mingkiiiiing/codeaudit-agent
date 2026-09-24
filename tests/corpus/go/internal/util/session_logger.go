// Package util 会话日志：干净反例。
package util

import "fmt"

// SessionLogger 会话日志器。
type SessionLogger struct {
	prefix string
}

// NewSessionLogger 构造。
func NewSessionLogger(prefix string) *SessionLogger {
	return &SessionLogger{prefix: prefix}
}

// Log 打印会话日志。
func (l *SessionLogger) Log(msg string) {
	fmt.Println(l.prefix + ": " + msg)
}

// Append 批量打印（具名调用进 goroutine：仍按普通调用点收集）。
func (l *SessionLogger) Append(msgs []string) {
	for _, m := range msgs {
		go l.Log(m)
	}
}
