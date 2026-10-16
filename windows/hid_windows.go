// Windows HID access: finds the ATK vendor settings interface and moves
// reports through overlapped I/O with timeouts.
package main

import (
	"errors"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unsafe"

	"vxeatk/core"
)

var (
	hidDLL                 = syscall.NewLazyDLL("hid.dll")
	cfgmgr32               = syscall.NewLazyDLL("cfgmgr32.dll")
	kernel32               = syscall.NewLazyDLL("kernel32.dll")
	pHidDGetHidGuid        = hidDLL.NewProc("HidD_GetHidGuid")
	pHidDGetPreparsedData  = hidDLL.NewProc("HidD_GetPreparsedData")
	pHidDFreePreparsedData = hidDLL.NewProc("HidD_FreePreparsedData")
	pHidDFlushQueue        = hidDLL.NewProc("HidD_FlushQueue")
	pHidPGetCaps           = hidDLL.NewProc("HidP_GetCaps")
	pCMGetIfaceListSizeW   = cfgmgr32.NewProc("CM_Get_Device_Interface_List_SizeW")
	pCMGetIfaceListW       = cfgmgr32.NewProc("CM_Get_Device_Interface_ListW")
	pCreateFileW           = kernel32.NewProc("CreateFileW")
	pReadFile              = kernel32.NewProc("ReadFile")
	pWriteFile             = kernel32.NewProc("WriteFile")
	pCloseHandle           = kernel32.NewProc("CloseHandle")
	pCreateEventW          = kernel32.NewProc("CreateEventW")
	pWaitForSingleObject   = kernel32.NewProc("WaitForSingleObject")
	pGetOverlappedResult   = kernel32.NewProc("GetOverlappedResult")
	pCancelIo              = kernel32.NewProc("CancelIo")
)

const (
	errIOPending = 997
	waitTimeout  = 0x102
)

type guid struct {
	d1     uint32
	d2, d3 uint16
	d4     [8]byte
}

type overlapped struct {
	internal, internalHigh uintptr
	offset, offsetHigh     uint32
	event                  uintptr
}

type hidCaps struct {
	usage, usagePage                 uint16
	inputLen, outputLen, featureLen  uint16
	reserved                         [17]uint16
	linkNodes, inButtons, inValues   uint16
	inData, outButtons, outValues    uint16
	outData, featButtons, featValues uint16
	featData                         uint16
}

// node is one ATK vendor settings interface.
type node struct {
	path     string
	vid, pid uint16
	family   string
}

var reVidPid = regexp.MustCompile(`(?i)VID_([0-9A-F]{4})&PID_([0-9A-F]{4})`)

func hidPaths() []string {
	var g guid
	pHidDGetHidGuid.Call(uintptr(unsafe.Pointer(&g)))
	var size uint32
	pCMGetIfaceListSizeW.Call(uintptr(unsafe.Pointer(&size)), uintptr(unsafe.Pointer(&g)), 0, 0)
	if size == 0 {
		return nil
	}
	buf := make([]uint16, size)
	pCMGetIfaceListW.Call(uintptr(unsafe.Pointer(&g)), 0, uintptr(unsafe.Pointer(&buf[0])), uintptr(size), 0)
	var out []string
	start := 0
	for i, c := range buf {
		if c == 0 {
			if i > start {
				out = append(out, syscall.UTF16ToString(buf[start:i]))
			}
			start = i + 1
		}
	}
	return out
}

func openPath(path string) (uintptr, error) {
	p, _ := syscall.UTF16PtrFromString(path)
	h, _, err := pCreateFileW.Call(uintptr(unsafe.Pointer(p)),
		0x80000000|0x40000000, // GENERIC_READ | GENERIC_WRITE
		0x1|0x2,               // FILE_SHARE_READ | FILE_SHARE_WRITE
		0, 3, 0x40000000, 0)   // OPEN_EXISTING, FILE_FLAG_OVERLAPPED
	if h == ^uintptr(0) {
		return 0, err
	}
	return h, nil
}

func caps(h uintptr) (hidCaps, bool) {
	var pre uintptr
	var c hidCaps
	if r, _, _ := pHidDGetPreparsedData.Call(h, uintptr(unsafe.Pointer(&pre))); r == 0 {
		return c, false
	}
	pHidPGetCaps.Call(pre, uintptr(unsafe.Pointer(&c)))
	pHidDFreePreparsedData.Call(pre)
	return c, true
}

// findNodes lists ATK settings interfaces. Both protocols use report ID 8:
// COMPX output reports are 16 bytes (17 with the ID), BITMOUSE 63 (64).
func findNodes(reg *core.Registry) []node {
	var out []node
	for _, path := range hidPaths() {
		m := reVidPid.FindStringSubmatch(path)
		if m == nil {
			continue
		}
		v, _ := strconv.ParseUint(m[1], 16, 16)
		p, _ := strconv.ParseUint(m[2], 16, 16)
		vid, pid := uint16(v), uint16(p)
		if vid != core.VIDs[0] && vid != core.VIDs[1] {
			continue
		}
		if reg != nil && reg.IsRecovery(vid, pid) {
			continue // a mouse in firmware-recovery mode: leave it alone
		}
		h, err := openPath(path)
		if err != nil {
			continue
		}
		c, ok := caps(h)
		pCloseHandle.Call(h)
		if !ok {
			continue
		}
		switch c.outputLen {
		case 17:
			out = append(out, node{path, vid, pid, "compx"})
		case 64:
			out = append(out, node{path, vid, pid, "bitmouse"})
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].path < out[j].path })
	return out
}

func nodesKey(ns []node) string {
	var b strings.Builder
	for _, n := range ns {
		b.WriteString(n.path + "|")
	}
	return b.String()
}

// hidTransport implements core.Transport over one open interface.
type hidTransport struct {
	h             uintptr
	event         uintptr
	inLen, outLen int
}

func openTransport(path string) (*hidTransport, error) {
	h, err := openPath(path)
	if err != nil {
		return nil, err
	}
	c, ok := caps(h)
	if !ok {
		pCloseHandle.Call(h)
		return nil, errors.New("can't read the mouse's HID description")
	}
	ev, _, _ := pCreateEventW.Call(0, 1, 0, 0)
	return &hidTransport{h: h, event: ev, inLen: int(c.inputLen), outLen: int(c.outputLen)}, nil
}

func (t *hidTransport) Close() {
	pCloseHandle.Call(t.h)
	pCloseHandle.Call(t.event)
}

func (t *hidTransport) io(proc *syscall.LazyProc, buf []byte, timeout time.Duration) (int, error) {
	ov := overlapped{event: t.event}
	var n uint32
	r, _, err := proc.Call(t.h, uintptr(unsafe.Pointer(&buf[0])), uintptr(len(buf)),
		uintptr(unsafe.Pointer(&n)), uintptr(unsafe.Pointer(&ov)))
	if r == 0 {
		if errno, ok := err.(syscall.Errno); !ok || errno != errIOPending {
			return 0, err
		}
		ms := uintptr(timeout / time.Millisecond)
		if w, _, _ := pWaitForSingleObject.Call(t.event, ms); w == waitTimeout {
			pCancelIo.Call(t.h)
			pGetOverlappedResult.Call(t.h, uintptr(unsafe.Pointer(&ov)), uintptr(unsafe.Pointer(&n)), 1)
			return 0, nil
		}
		if r, _, err := pGetOverlappedResult.Call(t.h, uintptr(unsafe.Pointer(&ov)),
			uintptr(unsafe.Pointer(&n)), 0); r == 0 {
			return 0, err
		}
	}
	return int(n), nil
}

func (t *hidTransport) Write(report []byte) error {
	buf := make([]byte, t.outLen) // Windows wants exactly the report length
	copy(buf, report)
	n, err := t.io(pWriteFile, buf, time.Second)
	if err != nil {
		return err
	}
	if n == 0 {
		return core.ErrAsleep
	}
	return nil
}

func (t *hidTransport) Read(timeout time.Duration) ([]byte, error) {
	buf := make([]byte, t.inLen)
	n, err := t.io(pReadFile, buf, timeout)
	if err != nil {
		return nil, err
	}
	if n == 0 {
		return nil, nil
	}
	return buf[:n], nil
}

func (t *hidTransport) Flush() { pHidDFlushQueue.Call(t.h) }

// openMouse opens the connected mouse, preferring a cable over a receiver.
func openMouse(reg *core.Registry) (core.Mouse, *hidTransport, error) {
	ns := findNodes(reg)
	if len(ns) == 0 {
		return nil, nil, errors.New("No ATK/VXE mouse found. Is the dongle plugged in?")
	}
	sort.SliceStable(ns, func(i, j int) bool {
		return !reg.IsReceiver(ns[i].vid, ns[i].pid) && reg.IsReceiver(ns[j].vid, ns[j].pid)
	})
	n := ns[0]
	t, err := openTransport(n.path)
	if err != nil {
		return nil, nil, err
	}
	if n.family == "compx" {
		return &core.CompxMouse{T: t, VID: n.vid, PID: n.pid, Receiver: reg.IsReceiver(n.vid, n.pid), Reg: reg}, t, nil
	}
	return core.NewBitMouse(t, n.vid, n.pid, reg), t, nil
}
