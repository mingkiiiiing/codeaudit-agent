// Package service 订单服务：干净反例（无注入/密钥/长函数）。
package service

import (
	"internal/model"
	"internal/util"
)

// OrderService 订单业务逻辑。
type OrderService struct {
	items []model.User
}

// NewOrderService 构造。
func NewOrderService() *OrderService {
	return &OrderService{}
}

// Add 增加订单项（跨包解析正例：model.New / util.Round）。
func (o *OrderService) Add(id int, price float64) string {
	o.items = append(o.items, model.New(id, "item"))
	return util.Round(price)
}

// Total 汇总。
func (o *OrderService) Total() float64 {
	sum := 0.0
	for range o.items {
		sum += 1.0
	}
	return sum
}
