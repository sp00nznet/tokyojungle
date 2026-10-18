#pragma once

#include <cstddef>
#include <cstdint>
#include <mutex>
#include <vector>

/* Each invocation owns its stack until return/unwind. Reuse is independent
 * of host thread identity, so nested callbacks and thread churn cannot alias. */
class TjCallbackStackPool {
    struct Slot { uint32_t base; bool used; };
    std::mutex mutex_;
    std::vector<Slot> slots_;
    uint32_t (*allocate_)(uint32_t);

public:
    class Lease {
        friend class TjCallbackStackPool;
        TjCallbackStackPool* owner_;
        size_t slot_;
        Lease(TjCallbackStackPool* owner, size_t slot, uint32_t address)
            : owner_(owner), slot_(slot), base(address) {}
    public:
        const uint32_t base;
        Lease(const Lease&) = delete;
        Lease& operator=(const Lease&) = delete;
        Lease(Lease&& other) noexcept
            : owner_(other.owner_), slot_(other.slot_), base(other.base) {
            other.owner_ = nullptr;
        }
        ~Lease() {
            if (owner_) {
                std::lock_guard<std::mutex> lock(owner_->mutex_);
                owner_->slots_[slot_].used = false;
            }
        }
    };

    explicit TjCallbackStackPool(uint32_t (*allocate)(uint32_t)) : allocate_(allocate) {}

    Lease acquire() {
        std::lock_guard<std::mutex> lock(mutex_);
        for (size_t i = 0; i < slots_.size(); ++i) {
            if (!slots_[i].used) {
                slots_[i].used = true;
                return Lease(this, i, slots_[i].base);
            }
        }
        // Grow metadata before consuming VM capacity so allocation failure
        // cannot lose a guest stack. No mutex stays held while guest code runs.
        slots_.push_back({0, false});
        const uint32_t base = allocate_(0x10000);
        if (!base) {
            slots_.pop_back();
            return Lease(nullptr, 0, 0);
        }
        slots_.back() = {base, true};
        return Lease(this, slots_.size() - 1, base);
    }
};
