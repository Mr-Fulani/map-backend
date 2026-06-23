"use client"

import * as React from "react"

import { cn } from "@/lib/utils"

const CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789`~,.<>?/;:][}{+_)(*&^%$#@!=-"
const BULLET = "•"
const ANIMATION_MS = 650
const STEPS = 18

type PasswordInputProps = Omit<React.ComponentProps<"input">, "type"> & {
  revealLabel?: string
  hideLabel?: string
}

function maskValue(length: number) {
  return BULLET.repeat(length)
}

function scrambleSegment(length: number) {
  return Array.from({ length }, () => CHARS[Math.floor(Math.random() * CHARS.length)]).join("")
}

const PasswordInput = React.forwardRef<HTMLInputElement, PasswordInputProps>(
  (
    {
      className,
      value,
      defaultValue,
      onChange,
      disabled,
      readOnly,
      revealLabel = "Показать пароль",
      hideLabel = "Скрыть пароль",
      ...props
    },
    ref
  ) => {
    const inputRef = React.useRef<HTMLInputElement>(null)
    const buttonRef = React.useRef<HTMLButtonElement>(null)
    const [visible, setVisible] = React.useState(false)
    const [animating, setAnimating] = React.useState(false)
    const [displayValue, setDisplayValue] = React.useState("")
    const [blink, setBlink] = React.useState(false)
    const [eyeOffset, setEyeOffset] = React.useState({ x: 0, y: 0 })
    const reduceMotion = React.useRef(false)
    const openMaskId = React.useId()

    React.useImperativeHandle(ref, () => inputRef.current as HTMLInputElement)

    const stringValue = String(value ?? defaultValue ?? "")
    const isControlled = value !== undefined
    const currentValue = isControlled ? String(value ?? "") : inputRef.current?.value ?? stringValue
    const inputType = visible || animating ? "text" : "password"
    const inputValue = animating ? displayValue : value

    React.useEffect(() => {
      reduceMotion.current = window.matchMedia("(prefers-reduced-motion: reduce)").matches
    }, [])

    React.useEffect(() => {
      if (!visible || animating) return
      setDisplayValue(currentValue)
    }, [animating, currentValue, visible])

    React.useEffect(() => {
      if (!visible || animating) return

      let blinkTimer: number
      let openTimer: number

      const scheduleBlink = () => {
        blinkTimer = window.setTimeout(() => {
          setBlink(true)
          openTimer = window.setTimeout(() => {
            setBlink(false)
            scheduleBlink()
          }, 120)
        }, 2200 + Math.random() * 4200)
      }

      scheduleBlink()

      return () => {
        window.clearTimeout(blinkTimer)
        window.clearTimeout(openTimer)
      }
    }, [animating, visible])

    React.useEffect(() => {
      if (disabled || readOnly) {
        setEyeOffset({ x: 0, y: 0 })
        return
      }

      let frame = 0

      function handleDocumentPointerMove(event: PointerEvent) {
        window.cancelAnimationFrame(frame)
        frame = window.requestAnimationFrame(() => {
          const bounds = buttonRef.current?.getBoundingClientRect()
          if (!bounds) return

          const centerX = bounds.left + bounds.width / 2
          const centerY = bounds.top + bounds.height / 2
          const x = Math.max(-5, Math.min(5, ((event.clientX - centerX) / 90) * 5))
          const y = Math.max(-4, Math.min(4, ((event.clientY - centerY) / 80) * 4))

          setEyeOffset({ x, y })
        })
      }

      document.addEventListener("pointermove", handleDocumentPointerMove, true)

      return () => {
        window.cancelAnimationFrame(frame)
        document.removeEventListener("pointermove", handleDocumentPointerMove, true)
      }
    }, [disabled, readOnly])

    function animateToggle(nextVisible: boolean) {
      const realValue = currentValue

      if (reduceMotion.current || realValue.length === 0) {
        setVisible(nextVisible)
        setAnimating(false)
        setDisplayValue(realValue)
        window.requestAnimationFrame(() => inputRef.current?.focus())
        return
      }

      setDisplayValue(nextVisible ? maskValue(realValue.length) : realValue)
      setAnimating(true)
      setBlink(!nextVisible)

      let step = 0
      const timer = window.setInterval(() => {
        step += 1
        const progress = Math.min(step / STEPS, 1)
        const revealed = Math.max(1, Math.round(realValue.length * progress))

        if (nextVisible) {
          const prefix = realValue.slice(0, Math.max(0, revealed - 1))
          const active = revealed < realValue.length ? scrambleSegment(1) : realValue.slice(revealed - 1, revealed)
          const suffix = maskValue(Math.max(0, realValue.length - revealed))
          setDisplayValue(`${prefix}${active}${suffix}`)
        } else {
          const hidden = Math.round(realValue.length * progress)
          const prefix = maskValue(hidden)
          const active = hidden < realValue.length ? scrambleSegment(1) : ""
          const suffix = realValue.slice(hidden + (active ? 1 : 0))
          setDisplayValue(`${prefix}${active}${suffix}`)
        }

        if (progress === 1) {
          window.clearInterval(timer)
          setVisible(nextVisible)
          setAnimating(false)
          setBlink(false)
          setDisplayValue(realValue)
          inputRef.current?.focus()
        }
      }, ANIMATION_MS / STEPS)
    }

    function handleChange(event: React.ChangeEvent<HTMLInputElement>) {
      if (!animating) setDisplayValue(event.target.value)
      onChange?.(event)
    }

    return (
      <div className="relative">
        <input
          {...props}
          ref={inputRef}
          type={inputType}
          value={inputValue}
          defaultValue={value === undefined ? defaultValue : undefined}
          onChange={handleChange}
          disabled={disabled}
          readOnly={readOnly || animating}
          className={cn(
            "flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 pr-11 text-base shadow-sm transition-colors file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 md:text-sm",
            className
          )}
        />
        <button
          ref={buttonRef}
          type="button"
          aria-label={visible ? hideLabel : revealLabel}
          aria-pressed={visible}
          title={visible ? hideLabel : revealLabel}
          disabled={disabled || readOnly}
          onClick={() => !animating && animateToggle(!visible)}
          className="absolute right-0 top-0 grid h-9 w-10 place-items-center rounded-r-md text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
        >
          <svg
            viewBox="0 0 24 24"
            aria-hidden="true"
            className="h-5 w-5"
            fill="none"
            xmlns="http://www.w3.org/2000/svg"
          >
            <defs>
              <mask id={openMaskId}>
                <path
                  d="M1 12C1 12 5 4 12 4C19 4 23 12 23 12V20H1V12Z"
                  fill="white"
                />
              </mask>
            </defs>
            <path
              d="M1 12C1 12 5 4 12 4C19 4 23 12 23 12"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeLinejoin="round"
              className={cn("origin-center transition-transform duration-150", blink && "translate-y-2 scale-y-0")}
            />
            <path
              d="M1 12C1 12 5 20 12 20C19 20 23 12 23 12"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeLinejoin="round"
              className={cn("origin-center transition-transform duration-150", !visible && !animating && "-translate-y-2 scale-y-0")}
            />
            <g mask={`url(#${openMaskId})`}>
              <g
                className="transition-transform duration-150"
                style={{
                  transform: `translate(${!blink ? eyeOffset.x : 0}px, ${!blink ? eyeOffset.y : 0}px)`,
                }}
              >
                <circle cx="12" cy="12" r="4" fill="currentColor" />
                <circle cx="13.2" cy="10.8" r="1" fill="hsl(var(--background))" />
              </g>
            </g>
          </svg>
          <span className="sr-only">{visible ? hideLabel : revealLabel}</span>
        </button>
      </div>
    )
  }
)

PasswordInput.displayName = "PasswordInput"

export { PasswordInput }
