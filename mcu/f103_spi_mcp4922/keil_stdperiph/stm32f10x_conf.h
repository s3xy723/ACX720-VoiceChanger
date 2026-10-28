/* ============================================================================
 *  stm32f10x_conf.h —— 标准库配置头（Keil 工程中含入即可）
 *  仅包含本工程用到的外设头；未定义 USE_FULL_ASSERT 时 assert_param 为空。
 * ==========================================================================*/
#ifndef __STM32F10X_CONF_H
#define __STM32F10X_CONF_H

#include "stm32f10x_rcc.h"
#include "stm32f10x_gpio.h"
#include "stm32f10x_spi.h"
#include "stm32f10x_exti.h"
#include "stm32f10x_tim.h"
#include "stm32f10x_usart.h"
#include "stm32f10x_flash.h"
#include "misc.h"               /* NVIC_Init */

#ifdef  USE_FULL_ASSERT
void assert_failed(uint8_t *file, uint32_t line);
#define assert_param(expr) ((expr) ? (void)0 : assert_failed((uint8_t *)__FILE__, __LINE__))
#else
#define assert_param(expr) ((void)0)
#endif

#endif /* __STM32F10X_CONF_H */
