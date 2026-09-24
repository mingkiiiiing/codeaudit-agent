// Package util 通用助手：干净反例。
package util

import "strings"

// Greet 问候语（跨包调用的目标符号）。
func Greet(name string) string {
	return "hello, " + name
}

// Mask 姓名脱敏。
func Mask(name string) string {
	if len(name) <= 1 {
		return "*"
	}
	stars := ""
	for i := 1; i < len(name); i++ {
		stars += "*"
	}
	return string(name[0]) + stars
}

// Summarize 词表摘要（report_service 跨包调用的目标符号）。
func Summarize(words []string) string {
	return strings.Join(words, ",")
}

// Round 保留两位小数（用整数运算模拟，避免引入 math 依赖说明）。
func Round(v float64) string {
	whole := int(v * 100)
	return format(whole)
}

// format 同文件助手（同文件调用不建边）。
func format(hundredths int) string {
	return itoa(hundredths/100) + "." + pad2(hundredths % 100)
}

// pad2 两位补零（语料内使用）。
func pad2(n int) string {
	if n < 10 {
		return "0" + itoa(n)
	}
	return itoa(n)
}

// itoa 极简整数转字符串（仅非负数，语料内使用）。
func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	digits := ""
	for n > 0 {
		digits = string(rune('0'+n%10)) + digits
		n /= 10
	}
	return digits
}
