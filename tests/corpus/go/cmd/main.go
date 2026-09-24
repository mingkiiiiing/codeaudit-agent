// Command corpus 组合入口：跨包调用（解析口径的正例集合）。
package main

import (
	"fmt"

	"internal/config"
	"internal/dao"
	"internal/service"
	"internal/util"
)

func main() {
	bucket := config.Describe()
	greeting := util.Greet("audit")
	users := dao.NewUserDao("users")
	audit := dao.NewAuditDAO("audit.db")
	svc := service.NewUserService(users, audit)
	orders := service.NewOrderService()
	orders.Add(1, 9.9)
	fmt.Println(bucket, greeting, svc, orders)
}
